from plexapi.server import PlexServer
import csv
import io
from difflib import SequenceMatcher
import re
from unidecode import unidecode
import os

def normalize_text(text):
    """Normalize text for better matching.
    - ASCII-fold accents
    - Lowercase
    - Replace '&' with 'and'
    - Remove punctuation
    - Collapse whitespace
    """
    if not isinstance(text, str):
        return ""
    t = unidecode(text)
    t = t.lower()
    t = t.replace('&', ' and ')
    t = re.sub(r"[\u2018\u2019'\"`]+", '', t)  # remove quotes/apostrophes (including curly)
    t = re.sub(r"[^a-z0-9\s]", ' ', t)  # remove other punctuation
    t = re.sub(r"\s+", ' ', t).strip()
    return t

def split_artists(artist: str):
    """Produce a list of possible artist tokens from a combined artist string."""
    if not artist:
        return []
    a = unidecode(artist)
    # Split on common separators
    parts = re.split(r";|,|\s+feat\.?\s+|\s+ft\.?\s+|\s+with\s+|\s*&\s*", a, flags=re.IGNORECASE)
    parts = [normalize_text(p) for p in parts if p and p.strip()]
    # Dedupe
    seen = set()
    out = []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out

def build_track_variations(track_name: str):
    if not track_name:
        return []
    originals = [track_name]
    # Remove common suffix patterns like (Live), [Remastered], - Acoustic, etc.
    base = re.split(r"\s*\(|\[| - ", track_name)[0].strip()
    originals.append(base)
    # Remove quotes/apostrophes
    originals.append(track_name.replace("'", '').replace('"', ''))
    # Swap straight and curly apostrophes
    originals.append(track_name.replace("'", "’"))
    originals.append(track_name.replace("’", "'"))
    # Replace & / and
    originals.append(track_name.replace('&', 'and'))
    originals.append(track_name.replace(' and ', ' & '))
    # Collapse spaces
    originals.extend([' '.join(o.split()) for o in list(originals)])
    # ASCII-fold
    originals.extend([unidecode(o) for o in list(originals)])
    # Deduplicate
    seen = set()
    out = []
    for o in originals:
        if o and o not in seen:
            seen.add(o)
            out.append(o)
    return out

def similarity_ratio(a, b):
    """Calculate similarity ratio between two strings"""
    return SequenceMatcher(None, a, b).ratio()

def find_best_match(track_name, artist_name, album_name, plex, library_name):
    """Find the best matching track in Plex library with improved matching for special cases.

    If track_name is missing, fall back to artist-only search and pick the best candidate by artist similarity.
    """
    if not artist_name or not plex:
        return None
    
    # First, try to find exact matches in the library
    music_library = plex.library.section(library_name)
    
    # If no track name provided, do an artist-only search
    if not track_name:
        try:
            results = music_library.searchTracks(artist=artist_name, maxresults=20)
        except Exception:
            results = []
        # Pick the best by artist similarity
        best_match = None
        best_score = 0.75
        artist_tokens = split_artists(artist_name)
        main_artist = artist_tokens[0] if artist_tokens else normalize_text(artist_name)
        for track in results:
            plex_artist = ''
            try:
                plex_artist = track.grandparentTitle if hasattr(track, 'grandparentTitle') else (track.artist().title if hasattr(track, 'artist') and track.artist() else '')
            except Exception:
                plex_artist = ''
            plex_artists = split_artists(plex_artist)
            plex_main_artist = plex_artists[0] if plex_artists else normalize_text(plex_artist)
            artist_score = similarity_ratio(main_artist, plex_main_artist)
            if artist_score > best_score:
                best_score = artist_score
                best_match = track
        return best_match if best_match else None

    # Generate search queries with different combinations (track provided)
    # Build query variations for robustness
    search_queries = []
    for tn in build_track_variations(track_name):
        search_queries.append(f"{tn} {artist_name}")
        search_queries.append(tn)
    # Also try first artist token
    artist_tokens = split_artists(artist_name)
    if artist_tokens:
        for tn in build_track_variations(track_name):
            search_queries.append(f"{tn} {artist_tokens[0]}")
    
    # Add album-specific searches if available
    if album_name:
        search_queries.extend([
            f"{track_name} {album_name}",
            f"{track_name} {artist_name} {album_name}",
        ])
    
    # Try each search query until we find a good match
    best_match = None
    best_score = 0.7  # Minimum threshold for a match
    
    for query in search_queries:
        try:
            # Search in the music library
            results = music_library.searchTracks(title=query, maxresults=30)
            
            # If no results, try with a more general search
            if not results and ' ' in query:
                # Try with just the first few words of the query
                partial_query = ' '.join(query.split()[:3])
                if partial_query != query:
                    results = music_library.searchTracks(title=partial_query, maxresults=30)

            # Broader fallback using library.search for tracks
            if not results:
                try:
                    broad_items = music_library.search(query, libtype='track', maxresults=30)
                    results = broad_items or []
                except Exception:
                    pass
            
            if not results:
                continue
            
            # Deduplicate by ratingKey
            seen_keys = set()
            deduped = []
            for t in results:
                rk = getattr(t, 'ratingKey', None)
                if rk is None or rk in seen_keys:
                    continue
                seen_keys.add(rk)
                deduped.append(t)
            results = deduped

            # Now use the existing matching logic on the search results
            normalized_artist = normalize_text(artist_name)
            artist_tokens = split_artists(artist_name)
            main_artist = artist_tokens[0] if artist_tokens else normalized_artist
            
            # Create multiple variations of the track name
            track_variations = build_track_variations(track_name)
            
            for track in results:
                plex_track = track.title
                plex_artist = track.grandparentTitle if hasattr(track, 'grandparentTitle') else ''
                
                for variation in track_variations:
                    normalized_track = normalize_text(variation)
                    normalized_plex_track = normalize_text(plex_track)
                    normalized_plex_artist = normalize_text(plex_artist)
                    
                    plex_artists = split_artists(plex_artist)
                    plex_main_artist = plex_artists[0] if plex_artists else normalized_plex_artist
                    
                    track_score = similarity_ratio(normalized_track, normalized_plex_track)
                    artist_score = similarity_ratio(normalized_artist, normalized_plex_artist)
                    main_artist_score = similarity_ratio(main_artist, plex_main_artist)
                    
                    if (normalized_track in normalized_plex_track or 
                        normalized_plex_track in normalized_track):
                        track_score = max(track_score, 0.8)
                    
                    common_patterns = [
                        (f'{normalized_track} {main_artist}', f'{normalized_plex_track} {plex_main_artist}'),
                        (f'{main_artist} {normalized_track}', f'{plex_main_artist} {normalized_plex_track}')
                    ]
                    
                    for pattern1, pattern2 in common_patterns:
                        if similarity_ratio(pattern1, pattern2) > 0.8:
                            track_score = max(track_score, 0.9)
                            break
                    
                    # Slight boost if album matches when provided
                    album_boost = 0.0
                    if album_name:
                        try:
                            plex_album = track.album().title if hasattr(track, 'album') and track.album() else ''
                        except Exception:
                            plex_album = ''
                        if normalize_text(album_name) and normalize_text(album_name) in normalize_text(plex_album):
                            album_boost = 0.05
                    effective_artist_score = max(artist_score, main_artist_score * 0.9)
                    total_score = (track_score * 0.6) + (effective_artist_score * 0.4) + album_boost
                    
                    if main_artist_score > 0.8 and track_score > 0.6:
                        total_score = max(total_score, 0.85)
                    
                    if total_score > best_score:
                        best_score = total_score
                        best_match = track
                        
                        if best_score > 0.9:
                            return best_match
                            
        except Exception as e:
            print(f"Error searching for '{query}': {str(e)}")
            continue
    
    return best_match if best_score >= 0.7 else None

def sync_playlist(plex_url, plex_token, library_name, playlist_name, csv_file):
    """Main function to sync playlist with progress tracking"""
    try:
        # Load the exported Spotify playlist CSV using csv module
        with open(csv_file, 'rb') as f:
            content = f.read()
            try:
                text = content.decode('utf-8-sig')
            except Exception:
                text = content.decode('utf-8')
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        
        # Connect to Plex
        plex = PlexServer(plex_url, plex_token)
        music_library = plex.library.section(library_name)

        # Delete existing playlist if it exists
        for pl in plex.playlists():
            if pl.title == playlist_name:
                pl.delete()
                break

        playlist = None
        missing_tracks = []
        found_tracks = 0
        total_tracks = len(rows)
        results = []

        # Process each track in the playlist
        for row in rows:
            track_name = row.get('Track Name', '')
            artist_name = row.get('Artist Name(s)', '')
            track_info = f"{track_name} - {artist_name}"
            
            try:
                # Generate base track name variations
                base_variations = set()
                
                # Original track name
                base_variations.add(track_name.strip())
                
                # Common patterns to remove
                patterns_to_remove = [
                    ' - From', ' - Live', ' - Acoustic', ' - Remastered',
                    ' - Remaster', ' - Single Version', ' - Album Version',
                    ' (feat.', ' (with ', ' (from ', ' [', ' (', ' - ', '...',
                    '"', "'"
                ]
                
                # Generate variations by removing patterns
                for pattern in patterns_to_remove:
                    for v in list(base_variations):
                        if pattern in v:
                            # Remove pattern and everything after
                            clean = v.split(pattern)[0].strip()
                            if clean:  # Only add non-empty variations
                                base_variations.add(clean)
                
                # Special character handling
                special_chars = {
                    'ø': 'o',
                    'é': 'e',
                    '&': 'and',
                    ' and ': ' & ',
                    "'": '',
                    '...': ' ',
                    '  ': ' '
                }
                
                # Add variations with special characters replaced
                for v in list(base_variations):
                    for char, replacement in special_chars.items():
                        if char in v:
                            new_variation = v.replace(char, replacement).strip()
                            if new_variation and new_variation != v:
                                base_variations.add(new_variation)
                
                # Generate artist variations
                artist_variations = set()
                artist_variations.add(artist_name.strip())
                
                # Main artist (first one listed)
                main_artist = artist_name.split(';')[0].split('&')[0].split('feat')[0].strip()
                if main_artist and main_artist != artist_name:
                    artist_variations.add(main_artist)
                
                # Generate search queries by combining track and artist variations
                search_queries = []
                
                # Try exact matches first
                for track_variant in base_variations:
                    for artist_variant in artist_variations:
                        search_queries.append(f'"{track_variant}" "{artist_variant}"')
                        search_queries.append(f'{track_variant} {artist_variant}')
                
                # Then try track name only (in case artist is in track name)
                for track_variant in base_variations:
                    search_queries.append(track_variant)
                
                # Remove duplicates while preserving order
                seen = set()
                search_queries = [q for q in search_queries if not (q in seen or seen.add(q))]
                
                # Execute searches until we get results
                search_results = []
                max_results = 30  # Increased for better matching
                
                for query in search_queries:
                    if not search_results and query.strip():
                        try:
                            results = music_library.searchTracks(
                                title=query,
                                maxresults=max_results
                            )
                            if results:
                                search_results = results
                                # Don't break, keep searching for better matches
                        except Exception as e:
                            continue  # Try next query if there's an error
                
                if search_results:
                    best_match = find_best_match(track_name, artist_name, search_results)
                    if best_match:
                        if playlist is None:
                            playlist = plex.createPlaylist(playlist_name, items=[best_match])
                        else:
                            playlist.addItems(best_match)
                        found_tracks += 1
                        results.append({
                            'status': 'success',
                            'track': track_info,
                            'match': f"{best_match.title} - {best_match.grandparentTitle}"
                        })
                        continue
                
                # If we get here, no match was found
                missing_tracks.append(track_info)
                results.append({
                    'status': 'missing',
                    'track': track_info,
                    'match': None
                })
                
            except Exception as e:
                results.append({
                    'status': 'error',
                    'track': track_info,
                    'error': str(e)
                })
                missing_tracks.append(f"{track_info} (error)")

        # Save missing songs to a text file
        if missing_tracks:
            with open('missing_tracks.txt', 'w', encoding='utf-8') as f:
                f.write("\n".join(missing_tracks))

        return {
            'status': 'completed',
            'total': total_tracks,
            'found': found_tracks,
            'missing': len(missing_tracks),
            'success_rate': (found_tracks / total_tracks) * 100 if total_tracks > 0 else 0,
            'results': results
        }

    except Exception as e:
        return {
            'status': 'error',
            'message': str(e)
        }

# This script is meant to be imported by app.py
# Configuration is now handled through the web interface