from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, Response, stream_with_context
from werkzeug.utils import secure_filename
import os
import json
import csv
import io
from functools import wraps
from plexapi.server import PlexServer
from plexapi.exceptions import NotFound, Unauthorized
from datetime import datetime
from flask_session import Session
from unidecode import unidecode

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'  # Change this to a secure secret key
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['ALLOWED_EXTENSIONS'] = {'csv'}  # Only allow CSV uploads
app.config['SESSION_TYPE'] = 'filesystem'  # Store sessions server-side to avoid large cookies
app.config['SESSION_PERMANENT'] = False
Session(app)

# Ensure upload folder exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Import the find_best_match function from plexsync
from plexsync import find_best_match

# Inject current time into all templates for use as {{ now }}
@app.context_processor
def inject_now():
    return {'now': datetime.now()}

# Format milliseconds to mm:ss string
def _format_duration_ms(ms):
    try:
        if not ms:
            return ''
        total_seconds = int(round(ms / 1000))
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        return f"{minutes}:{seconds:02d}"
    except Exception:
        return ''

# Build robust query variants to handle apostrophes and special characters
def _query_variants(text: str) -> list[str]:
    try:
        base = (text or '').strip()
        if not base:
            return []
        variants = []
        # Original
        variants.append(base)
        # ASCII fold
        folded = unidecode(base)
        if folded != base:
            variants.append(folded)
        # Swap straight and curly apostrophes
        swapped_curly = base.replace("'", "’")
        swapped_straight = base.replace("’", "'")
        if swapped_curly not in variants:
            variants.append(swapped_curly)
        if swapped_straight not in variants:
            variants.append(swapped_straight)
        # Remove apostrophes and quotes
        no_quotes = folded.replace("'", '').replace('"', '')
        if no_quotes not in variants:
            variants.append(no_quotes)
        # Replace & with and and vice versa
        amp_to_and = no_quotes.replace('&', 'and')
        if amp_to_and not in variants:
            variants.append(amp_to_and)
        and_to_amp = amp_to_and.replace(' and ', ' & ')
        if and_to_amp not in variants:
            variants.append(and_to_amp)
        # Remove content in parentheses/brackets and after dashes
        simple = and_to_amp.split(' (')[0].split(' [')[0].split(' - ')[0].strip()
        if simple and simple not in variants:
            variants.append(simple)
        # Collapse multiple spaces
        collapsed = ' '.join(simple.split())
        if collapsed and collapsed not in variants:
            variants.append(collapsed)
        # Deduplicate while preserving order
        seen = set()
        uniq = []
        for v in variants:
            if v and v not in seen:
                seen.add(v)
                uniq.append(v)
        return uniq
    except Exception:
        return [text] if text else []

# Handle favicon requests to avoid 404s in logs
@app.route('/favicon.ico')
def favicon():
    # Return 204 No Content; optionally place a file at static/favicon.ico and redirect to it
    return ('', 204)

# Login required decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'config' not in session:
            flash('Please configure your Plex server first', 'error')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        # Save configuration
        # Checkbox sends 'on' when checked and is absent when unchecked
        unified_playlist = request.form.get('unified_playlist') == 'on'
        
        # Initialize session data structure
        session['config'] = {
            'PLEX_BASE_URL': request.form.get('plex_url', '').strip(),
            'PLEX_TOKEN': request.form.get('plex_token', '').strip(),
            'MUSIC_LIBRARY_NAME': 'Music',  # Default value, will be updated later
            'UNIFIED_PLAYLIST': unified_playlist
        }
        
        # Handle file uploads
        if 'files' not in request.files:
            flash('No file part', 'error')
            return redirect(request.url)
        
        files = request.files.getlist('files')
        if not files or not any(f.filename for f in files):
            flash('No selected files', 'error')
            return redirect(request.url)
        
        valid_files = []
        all_records = []
        file_playlists = {}
        
        for file in files:
            if file and file.filename and allowed_file(file.filename):
                try:
                    # Read and validate the CSV file
                    content = file.read()
                    try:
                        text = content.decode('utf-8-sig')
                    except Exception:
                        text = content.decode('utf-8')
                    
                    reader = csv.DictReader(io.StringIO(text))
                    required_columns = ['Artist Name(s)']
                    fieldnames = reader.fieldnames or []
                    
                    if not all(col in fieldnames for col in required_columns):
                        flash(f'File {file.filename} is missing required columns. Must contain at least "Artist Name(s)"', 'error')
                        continue
                    
                    # Save the file
                    filename = secure_filename(file.filename)
                    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                    
                    # Make sure we don't overwrite existing files
                    counter = 1
                    base, ext = os.path.splitext(filename)
                    while os.path.exists(filepath):
                        filename = f"{base}_{counter}{ext}"
                        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                        counter += 1
                    
                    # Save the file
                    file.stream.seek(0)
                    file.save(filepath)
                    
                    # Read the records
                    reader = csv.DictReader(io.StringIO(text))
                    records = list(reader)
                    
                    # Store file info and records
                    playlist_name = os.path.splitext(filename)[0].replace('_', ' ').strip()
                    file_info = {
                        'filename': filename,
                        'filepath': filepath,
                        'playlist_name': playlist_name,
                        'track_count': len(records)
                    }
                    valid_files.append(file_info)
                    
                    # Store records with file reference
                    for record in records:
                        record['_source_file'] = filename
                    all_records.extend(records)
                    
                except Exception as e:
                    flash(f'Error processing file {file.filename}: {str(e)}', 'error')
            else:
                flash(f'Invalid file type for {file.filename}. Only CSV files are allowed.', 'error')
        
        if not valid_files:
            flash('No valid CSV files were uploaded.', 'error')
            return redirect(request.url)
        
        # Store the files and records in the session
        session['uploaded_files'] = valid_files
        session['tracks'] = all_records
        session['total_tracks'] = len(all_records)
        
        # If unified playlist, set the playlist name to the first file's name
        if unified_playlist and valid_files:
            session['config']['PLAYLIST_NAME'] = valid_files[0]['playlist_name']
        
        return redirect(url_for('match_tracks'))
    
    return render_template('index.html', config=session.get('config', {}))

@app.route('/configure', methods=['GET', 'POST'])
@login_required
def configure():
    config = session.get('config', {})
    
    if request.method == 'POST':
        # Update config from form
        playlist_name = (request.form.get('playlist_name', 'My Playlist') or 'My Playlist').strip().replace('_', ' ')
        config.update({
            'PLEX_BASE_URL': request.form.get('plex_url', '').strip(),
            'PLEX_TOKEN': request.form.get('plex_token', '').strip(),
            'MUSIC_LIBRARY_NAME': request.form.get('library_name', 'Music').strip(),
            'PLAYLIST_NAME': playlist_name
        })
        session['config'] = config
        flash('Configuration updated!', 'success')
    
    return render_template('configure.html', config=config)

def generate_sync_progress(config, csv_file):
    try:
        # Initialize Plex connection
        plex = PlexServer(config['PLEX_BASE_URL'], config['PLEX_TOKEN'])
        
        # Read the CSV file
        try:
            tracks = []
            with open(csv_file, 'rb') as f:
                content = f.read()
                try:
                    text = content.decode('utf-8-sig')
                except Exception:
                    text = content.decode('utf-8')
            reader = csv.DictReader(io.StringIO(text))
            for row in reader:
                tracks.append({
                    'title': row.get('Track Name', ''),
                    'artist': row.get('Artist Name(s)', ''),
                    'album': row.get('Album Name', '')
                })
        except Exception as e:
            yield json.dumps({
                'status': 'error',
                'message': f'Error reading CSV file: {str(e)}'
            }) + '\n'
            return
        
        # Initialize counters
        total_tracks = len(tracks)
        found_tracks = []
        missing_tracks = []
        
        # Process each track
        for i, track in enumerate(tracks, 1):
            progress = int((i / total_tracks) * 100)
            track_info = f"{track.get('title', 'Unknown')} - {track.get('artist', 'Unknown')}"
            
            # Update progress
            yield json.dumps({
                'status': 'processing',
                'progress': progress,
                'track': track_info,
                'message': f'Processing track {i} of {total_tracks}'
            }) + '\n'
            try:
                # Try to find the best match in Plex
                matched_track = find_best_match(
                    track.get('title', ''),
                    track.get('artist', ''),
                    track.get('album', ''),
                    plex,
                    config['MUSIC_LIBRARY_NAME']
                )
                
                if matched_track:
                    found_tracks.append(matched_track)
                    yield json.dumps({
                        'status': 'found',
                        'track': track_info,
                        'match': f"{matched_track.title} - {matched_track.artist().title if matched_track.artist() else 'Unknown'}"
                    }) + '\n'
                else:
                    missing_tracks.append({
                        'title': track.get('title', 'Unknown'),
                        'artist': track.get('artist', 'Unknown'),
                        'album': track.get('album', '')
                    })
                    yield json.dumps({
                        'status': 'missing',
                        'track': track_info
                    }) + '\n'
            except Exception as e:
                yield json.dumps({
                    'status': 'error',
                    'track': track_info,
                    'message': str(e),
                    'details': str(e)
                }) + '\n'
        # Create or update the playlist with found tracks
        if found_tracks:
            try:
                # Remove duplicate tracks while preserving order
                unique_tracks = []
                seen = set()
                for track in found_tracks:
                    if track.ratingKey not in seen:
                        seen.add(track.ratingKey)
                        unique_tracks.append(track)
                
                # Create or update the playlist
                try:
                    playlist = plex.playlist(config['PLAYLIST_NAME'])
                    # Clear existing items and add new ones
                    playlist.removeItems(playlist.items())
                    playlist.addItems(unique_tracks)
                except NotFound:
                    # Create a new playlist if it doesn't exist
                    playlist = plex.createPlaylist(config['PLAYLIST_NAME'], items=unique_tracks)
                
                # Final success message
                yield json.dumps({
                    'status': 'completed',
                    'found': len(unique_tracks),
                    'missing': len(missing_tracks),
                    'message': f'Successfully created/updated playlist "{config["PLAYLIST_NAME"]}" with {len(unique_tracks)} tracks.'
                }) + '\n'
            except Exception as e:
                yield json.dumps({
                    'status': 'error',
                    'message': f'Error creating/updating playlist: {str(e)}',
                    'details': str(e)
                }) + '\n'
        # If we have missing tracks, return them
        if missing_tracks:
            yield json.dumps({
                'status': 'missing_tracks',
                'found': len(found_tracks) - len(missing_tracks),
                'missing': len(missing_tracks),
                'missing_tracks': missing_tracks,
                'message': f'Found {len(found_tracks) - len(missing_tracks)} tracks, but {len(missing_tracks)} were not found.'
            }) + '\n'
    except Unauthorized:
        yield json.dumps({
            'status': 'error',
            'message': 'Unauthorized: Invalid Plex token',
            'details': 'Please check your Plex token and try again.'
        }) + '\n'
    except Exception as e:
        yield json.dumps({
            'status': 'error',
            'message': f'An error occurred: {str(e)}',
            'details': str(e)
        }) + '\n'

@app.route('/run_sync', methods=['POST'])
@login_required
def run_sync():
    config = session.get('config', {})
    csv_file = session.get('csv_file')
    
    if not csv_file or not os.path.exists(csv_file):
        return jsonify({'status': 'error', 'message': 'No valid CSV file found. Please upload again.'})
    
    # Validate required config
    required = ['PLEX_BASE_URL', 'PLEX_TOKEN', 'MUSIC_LIBRARY_NAME', 'PLAYLIST_NAME']
    if not all(config.get(field) for field in required):
        return jsonify({'status': 'error', 'message': 'Missing required configuration'})
    
    # Return a streaming response for progress updates
    return Response(
        stream_with_context(generate_sync_progress(config, csv_file)),
        mimetype='text/event-stream'
    )

@app.route('/test_connection', methods=['POST'])
@login_required
def test_connection():
    data = request.get_json()
    plex_url = data.get('plex_url')
    plex_token = data.get('plex_token')
    
    if not plex_url or not plex_token:
        return jsonify({'success': False, 'message': 'Missing Plex URL or token'}), 400
    
    try:
        # Try to connect to Plex server
        plex = PlexServer(plex_url, plex_token, timeout=10)
        server_name = plex.friendlyName
        return jsonify({
            'success': True,
            'server_name': server_name,
            'message': f'Successfully connected to {server_name}'
        })
    except Unauthorized:
        return jsonify({'success': False, 'message': 'Invalid Plex token'}), 401
    except Exception as e:
        return jsonify({'success': False, 'message': f'Failed to connect: {str(e)}'}), 500

@app.route('/search_plex', methods=['POST'])
@login_required
def search_plex():
    config = session.get('config', {})
    data = request.get_json()
    query = data.get('query', '').strip()
    original_artist = (data.get('original_artist') or '').strip()
    
    if not query and not original_artist:
        return jsonify({'success': False, 'message': 'Enter a track or artist to search'}), 200
    
    try:
        plex = PlexServer(config['PLEX_BASE_URL'], config['PLEX_TOKEN'])
        
        # Search for tracks in the music library
        library_name = config.get('MUSIC_LIBRARY_NAME') or 'Music'
        try:
            library = plex.library.section(library_name)
        except Exception as e:
            return jsonify({
                'success': False,
                'message': f'Music library "{library_name}" not found. Please check your configuration.'
            }), 200
        results = []
        tried = set()
        # Try multiple variants of the provided query
        for q in _query_variants(query):
            if q in tried:
                continue
            tried.add(q)
            try:
                res = library.searchTracks(title=q, maxresults=20)
                if res:
                    results.extend(res)
            except Exception:
                pass
        if not results and original_artist:
            try:
                results = library.searchTracks(artist=original_artist, maxresults=20)
            except Exception:
                results = []
        if not results:
            # Fallback broad search; filter tracks only
            try:
                broad_query = (query or original_artist)
                # Try variants for broad search too
                items = []
                for q in _query_variants(broad_query):
                    res = library.search(q, libtype='track', maxresults=20)
                    if res:
                        items.extend(res)
                results = items or []
            except Exception:
                results = []
        # Deduplicate results by ratingKey while preserving order
        deduped = []
        seen_keys = set()
        for t in results:
            rk = getattr(t, 'ratingKey', None)
            if rk is None or rk in seen_keys:
                continue
            seen_keys.add(rk)
            deduped.append(t)
        results = deduped
        
        # Format results for the UI
        formatted_results = []
        for track in results:
            try:
                artist_obj = track.artist() if hasattr(track, 'artist') else None
            except Exception:
                artist_obj = None
            try:
                album_obj = track.album() if hasattr(track, 'album') else None
            except Exception:
                album_obj = None

            formatted_results.append({
                'title': getattr(track, 'title', 'Unknown'),
                'artist': getattr(artist_obj, 'title', 'Unknown') if artist_obj else 'Unknown',
                'album': getattr(album_obj, 'title', 'Unknown') if album_obj else 'Unknown',
                'year': getattr(track, 'year', None),
                'duration': _format_duration_ms(getattr(track, 'duration', None)),
                'ratingKey': getattr(track, 'ratingKey', None),
                'thumb': getattr(track, 'thumbUrl', None) if hasattr(track, 'thumbUrl') else None,
                'albumArtist': getattr(track, 'originalTitle', '') or (getattr(album_obj, 'originalTitle', '') if album_obj else '')
            })
        
        return jsonify({
            'success': True,
            'results': formatted_results
        })
    except Unauthorized:
        return jsonify({'success': False, 'message': 'Invalid Plex token. Please re-enter your token.'}), 200
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Search failed: {str(e)}'
        }), 200

@app.route('/add_to_playlist', methods=['POST'])
@login_required
def add_to_playlist():
    config = session.get('config', {})
    data = request.get_json()
    track_key = data.get('track_key')
    original_track = data.get('original_track')
    
    if not track_key or not original_track:
        return jsonify({'success': False, 'message': 'Missing track information'}), 400
    
    try:
        plex = PlexServer(config['PLEX_BASE_URL'], config['PLEX_TOKEN'])
        
        # Get the track from Plex
        track = plex.fetchItem(int(track_key))
        
        # Get or create the playlist
        try:
            playlist = plex.playlist(config['PLAYLIST_NAME'])
        except NotFound:
            # Create a new playlist if it doesn't exist
            playlist = plex.createPlaylist(config['PLAYLIST_NAME'], items=[])
        
        # Add the track to the playlist if not already present
        if track.ratingKey not in [item.ratingKey for item in playlist.items()]:
            playlist.addItems([track])
        
        return jsonify({
            'success': True,
            'message': f'Added \"{track.title}\" to playlist',
            'track': {
                'title': track.title,
                'artist': track.artist().title if track.artist() else 'Unknown',
                'album': track.album().title if track.album() else 'Unknown'
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Failed to add track to playlist: {str(e)}'
        }), 500

@app.route('/match-tracks')
@login_required
def match_tracks():
    config = session.get('config', {})
    tracks = session.get('tracks', [])
    uploaded_files = session.get('uploaded_files', [])
    unified_playlist = config.get('UNIFIED_PLAYLIST', True)
    
    if not tracks:
        flash('No tracks found in the uploaded files.', 'error')
        return redirect(url_for('index'))
    
    # If separate playlists workflow, go to the first file page
    if not unified_playlist and uploaded_files:
        return redirect(url_for('match_tracks_file', file_index=0))
    
    # Attempt lightweight auto-matching to reduce missing list
    total_tracks = len(tracks)
    found_count = 0
    missing_by_file = []
    
    # Prepare a lookup for files
    files_by_name = {f['filename']: f for f in uploaded_files}
    per_file_missing = {f['filename']: [] for f in uploaded_files} if uploaded_files else {'ALL': []}
    
    # Try to connect to Plex and pre-match
    plex = None
    music_library = None
    try:
        if config.get('PLEX_BASE_URL') and config.get('PLEX_TOKEN'):
            plex = PlexServer(config['PLEX_BASE_URL'], config['PLEX_TOKEN'])
            music_library = plex.library.section(config.get('MUSIC_LIBRARY_NAME', 'Music'))
    except Exception:
        plex = None
        music_library = None
    
    from plexsync import find_best_match  # local import to avoid circulars at top
    
    for t in tracks:
        src = t.get('_source_file') or 'ALL'
        artist = t.get('Artist Name(s)', '')
        title = t.get('Track Name', '')
        album = t.get('Album Name', '')
        matched = None
        if plex and music_library:
            try:
                matched = find_best_match(title, artist, album, plex, config.get('MUSIC_LIBRARY_NAME', 'Music'))
            except Exception:
                matched = None
        if matched:
            found_count += 1
        else:
            per_file_missing.setdefault(src, []).append(t)
    
    # Build missing_by_file structure for template
    if uploaded_files:
        for f in uploaded_files:
            missing_list = per_file_missing.get(f['filename'], [])
            missing_by_file.append({
                'filename': f['filename'],
                'playlist_name': f.get('playlist_name') or os.path.splitext(f['filename'])[0].replace('_', ' ').strip(),
                'missing_tracks': missing_list,
                'missing_count': len(missing_list)
            })
    else:
        # Single list
        missing_list = per_file_missing.get('ALL', [])
        missing_by_file.append({
            'filename': 'ALL',
            'playlist_name': config.get('PLAYLIST_NAME', 'Playlist'),
            'missing_tracks': missing_list,
            'missing_count': len(missing_list)
        })
    
    return render_template('match_tracks.html', 
                         config=config, 
                         uploaded_files=uploaded_files,
                         unified_playlist=unified_playlist,
                         total_tracks=total_tracks,
                         found_count=found_count,
                         missing_by_file=missing_by_file,
                         file_mode=False,
                         file_index=None,
                         total_files=len(uploaded_files) if uploaded_files else 1)

@app.route('/match-tracks/<int:file_index>')
@login_required
def match_tracks_file(file_index: int):
    config = session.get('config', {})
    tracks = session.get('tracks', [])
    uploaded_files = session.get('uploaded_files', [])
    unified_playlist = config.get('UNIFIED_PLAYLIST', True)
    
    if not uploaded_files or file_index < 0 or file_index >= len(uploaded_files):
        return redirect(url_for('match_tracks'))
    
    current_file = uploaded_files[file_index]
    filename = current_file['filename']
    file_tracks = [t for t in tracks if t.get('_source_file') == filename]
    
    total_tracks = len(file_tracks)
    found_count = 0
    missing_by_file = []
    
    # Try to connect to Plex
    plex = None
    music_library = None
    try:
        if config.get('PLEX_BASE_URL') and config.get('PLEX_TOKEN'):
            plex = PlexServer(config['PLEX_BASE_URL'], config['PLEX_TOKEN'])
            music_library = plex.library.section(config.get('MUSIC_LIBRARY_NAME', 'Music'))
    except Exception:
        plex = None
        music_library = None
    
    from plexsync import find_best_match
    per_file_missing = []
    for t in file_tracks:
        artist = t.get('Artist Name(s)', '')
        title = t.get('Track Name', '')
        album = t.get('Album Name', '')
        matched = None
        if plex and music_library:
            try:
                matched = find_best_match(title, artist, album, plex, config.get('MUSIC_LIBRARY_NAME', 'Music'))
            except Exception:
                matched = None
        if matched:
            found_count += 1
        else:
            per_file_missing.append(t)
    
    missing_by_file.append({
        'filename': filename,
        'playlist_name': current_file.get('playlist_name') or os.path.splitext(filename)[0].replace('_', ' ').strip(),
        'missing_tracks': per_file_missing,
        'missing_count': len(per_file_missing)
    })
    
    return render_template('match_tracks.html',
                           config=config,
                           uploaded_files=uploaded_files,
                           unified_playlist=False,  # in file-mode, we are handling separate playlists
                           total_tracks=total_tracks,
                           found_count=found_count,
                           missing_by_file=missing_by_file,
                           file_mode=True,
                           file_index=file_index,
                           total_files=len(uploaded_files))

@app.route('/create-playlist', methods=['POST'])
@login_required
def create_playlist():
    try:
        config = session.get('config', {})
        tracks = session.get('tracks', [])
        uploaded_files = session.get('uploaded_files', [])
        unified_playlist = config.get('UNIFIED_PLAYLIST', True)
        created_playlists = session.get('created_playlists', [])
        
        if not tracks or not uploaded_files:
            flash('No tracks found in the uploaded files.', 'error')
            return redirect(url_for('index'))
        
        # Connect to Plex
        try:
            plex = PlexServer(config['PLEX_BASE_URL'], config['PLEX_TOKEN'])
            music_library = plex.library.section(config.get('MUSIC_LIBRARY_NAME', 'Music'))
        except Exception as e:
            flash(f'Error connecting to Plex: {str(e)}', 'error')
            return redirect(url_for('index'))
        
        # Optional per-file index for sequential workflow
        file_index_str = request.form.get('file_index')
        per_file_mode = (not unified_playlist) and (file_index_str is not None)
        next_index = None
        only_selected = bool(request.form.get('only_selected'))
        
        if per_file_mode:
            try:
                current_index = int(file_index_str)
            except Exception:
                current_index = 0
            if current_index < 0 or current_index >= len(uploaded_files):
                return redirect(url_for('match_tracks'))
            target_files = [uploaded_files[current_index]]
            next_index = current_index + 1 if current_index + 1 < len(uploaded_files) else None
        else:
            target_files = uploaded_files if not unified_playlist else None
        
        if unified_playlist:
            # Create a single unified playlist from all files
            playlist_name = request.form.get('playlist_name', config.get('PLAYLIST_NAME', 'Imported Playlist'))
            # Use selected ratingKeys if provided
            rk_list = request.form.getlist('track_ratingKey[]')
            matched_tracks = []
            
            if rk_list:
                for rk in rk_list:
                    try:
                        item = plex.fetchItem(int(rk))
                        matched_tracks.append(item)
                    except Exception:
                        pass
            elif not only_selected:
                # Process all tracks together (fallback)
                for track in tracks:
                    artist = track.get('Artist Name(s)', '')
                    title = track.get('Track Name', '')
                    album = track.get('Album Name', '')
                    
                    best_match = find_best_match(title, artist, album, plex, config.get('MUSIC_LIBRARY_NAME', 'Music'))
                    if best_match:
                        matched_tracks.append(best_match)
            
            if matched_tracks:
                # Remove duplicate tracks while preserving order
                seen = set()
                unique_tracks = []
                for track in matched_tracks:
                    if track.ratingKey not in seen:
                        seen.add(track.ratingKey)
                        unique_tracks.append(track)
                
                # Create the playlist
                playlist = music_library.createPlaylist(playlist_name, items=unique_tracks)
                created_playlists.append({
                    'name': playlist.title,
                    'track_count': len(unique_tracks),
                    'source': 'Multiple files'
                })
        elif per_file_mode:
            # Create a playlist for a single file (sequential workflow)
            f = target_files[0]
            filename = f['filename']
            default_name = os.path.splitext(filename)[0].replace('_', ' ').strip()
            playlist_name = (request.form.get('playlist_name') or default_name).strip()
            file_tracks = [t for t in tracks if t.get('_source_file') == filename]
            rk_list = request.form.getlist('track_ratingKey[]')
            matched_tracks = []
            
            if rk_list:
                for rk in rk_list:
                    try:
                        item = plex.fetchItem(int(rk))
                        matched_tracks.append(item)
                    except Exception:
                        pass
            elif not only_selected:
                for track in file_tracks:
                    artist = track.get('Artist Name(s)', '')
                    title = track.get('Track Name', '')
                    album = track.get('Album Name', '')
                    best_match = find_best_match(title, artist, album, plex, config.get('MUSIC_LIBRARY_NAME', 'Music'))
                    if best_match:
                        matched_tracks.append(best_match)
            
            if matched_tracks:
                seen = set()
                unique_tracks = []
                for track in matched_tracks:
                    if track.ratingKey not in seen:
                        seen.add(track.ratingKey)
                        unique_tracks.append(track)
                playlist = music_library.createPlaylist(playlist_name, items=unique_tracks)
                created_playlists.append({
                    'name': playlist.title,
                    'track_count': len(unique_tracks),
                    'source': filename
                })
            # Store progress and decide where to go next
            session['created_playlists'] = created_playlists
            if next_index is not None:
                return redirect(url_for('match_tracks_file', file_index=next_index))
            # Finalize when last file done
            # Cleanup: delete uploaded files and clear session data for uploads/tracks
            try:
                for f2 in uploaded_files or []:
                    try:
                        path = f2.get('filepath')
                        if path and os.path.exists(path):
                            os.remove(path)
                    except Exception:
                        pass
            finally:
                session.pop('uploaded_files', None)
                session.pop('tracks', None)
                session.pop('total_tracks', None)
            return redirect(url_for('playlist_created'))
        else:
            # Create separate playlists for each file
            for file_info in uploaded_files:
                filename = file_info['filename']
                playlist_name = os.path.splitext(filename)[0].replace('_', ' ').strip()
                file_tracks = [t for t in tracks if t.get('_source_file') == filename]
                rk_list = request.form.getlist('track_ratingKey[]')
                matched_tracks = []
                
                if rk_list:
                    for rk in rk_list:
                        try:
                            item = plex.fetchItem(int(rk))
                            matched_tracks.append(item)
                        except Exception:
                            pass
                elif not only_selected:
                    for track in file_tracks:
                        artist = track.get('Artist Name(s)', '')
                        title = track.get('Track Name', '')
                        album = track.get('Album Name', '')
                        
                        best_match = find_best_match(title, artist, album, plex, config.get('MUSIC_LIBRARY_NAME', 'Music'))
                        if best_match:
                            matched_tracks.append(best_match)
                
                if matched_tracks:
                    # Remove duplicate tracks while preserving order
                    seen = set()
                    unique_tracks = []
                    for track in matched_tracks:
                        if track.ratingKey not in seen:
                            seen.add(track.ratingKey)
                            unique_tracks.append(track)
                    
                    # Create the playlist
                    playlist = music_library.createPlaylist(playlist_name, items=unique_tracks)
                    created_playlists.append({
                        'name': playlist.title,
                        'track_count': len(unique_tracks),
                        'source': filename
                    })
        
        if not created_playlists:
            flash('No matching tracks found in your Plex library.', 'error')
            return redirect(url_for('index'))
        
        # For unified or all-at-once separate flow, cleanup now and store created playlists
        try:
            for f in uploaded_files or []:
                try:
                    path = f.get('filepath')
                    if path and os.path.exists(path):
                        os.remove(path)
                except Exception:
                    pass
        finally:
            session.pop('uploaded_files', None)
            session.pop('tracks', None)
            session.pop('total_tracks', None)
        session['created_playlists'] = created_playlists
        
        return redirect(url_for('playlist_created'))
            
    except Exception as e:
        flash(f'An error occurred: {str(e)}', 'error')
        return redirect(url_for('index'))

@app.route('/playlist-created')
@login_required
def playlist_created():
    playlists = session.get('created_playlists')
    if not playlists:
        flash('No playlists were created.', 'error')
        return redirect(url_for('index'))
    
    return render_template('playlist_created.html', 
                         playlists=playlists,
                         unified_playlist=len(playlists) == 1 and 'source' not in playlists[0])

if __name__ == '__main__':
    # Allow configuring host/port via environment for Docker
    port = int(os.getenv('PORT', '5000'))
    debug = os.getenv('FLASK_DEBUG', '0') == '1'
    app.run(host='0.0.0.0', port=port, debug=debug)
