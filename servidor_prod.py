from flask import Flask, request, jsonify, redirect, session, url_for
from flask_cors import CORS
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import os, json, tempfile, uuid

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'nexus-secret-key-change-this')
CORS(app, origins="*")

SCOPES = ['https://www.googleapis.com/auth/drive']

# En Railway las credenciales van como variable de entorno
def get_client_config():
    config_str = os.environ.get('GOOGLE_CREDENTIALS')
    if config_str:
        return json.loads(config_str)
    # Fallback local
    with open('credentials.json') as f:
        return json.load(f)

def get_drive_service(token_json):
    creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build('drive', 'v3', credentials=creds)

# ── AUTH ──
@app.route('/auth/login')
def auth_login():
    config = get_client_config()
    redirect_uri = request.args.get('redirect_uri', url_for('auth_callback', _external=True))
    flow = Flow.from_client_config(config, scopes=SCOPES, redirect_uri=redirect_uri)
    auth_url, state = flow.authorization_url(access_type='offline', include_granted_scopes='true')
    session['state'] = state
    session['redirect_uri'] = redirect_uri
    return jsonify({'auth_url': auth_url})

@app.route('/auth/callback')
def auth_callback():
    config = get_client_config()
    redirect_uri = session.get('redirect_uri', url_for('auth_callback', _external=True))
    flow = Flow.from_client_config(config, scopes=SCOPES, state=session.get('state'), redirect_uri=redirect_uri)
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    token_json = creds.to_json()
    # Redirige al frontend con el token
    frontend = os.environ.get('FRONTEND_URL', 'http://localhost')
    return redirect(f"{frontend}?token={token_json}")

@app.route('/auth/refresh', methods=['POST'])
def auth_refresh():
    try:
        data = request.json
        creds = Credentials.from_authorized_user_info(json.loads(data['token']), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return jsonify({'token': creds.to_json(), 'valid': True})
    except Exception as e:
        return jsonify({'error': str(e), 'valid': False}), 400

# ── DRIVE ENDPOINTS (requieren token en header) ──
def get_token():
    return request.headers.get('X-Drive-Token') or request.args.get('token')

@app.route('/drive/list')
def list_files():
    try:
        token = get_token()
        if not token: return jsonify({'error': 'No token'}), 401
        folder_id = request.args.get('folder_id', 'root')
        service = get_drive_service(token)
        results = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            pageSize=50, orderBy="folder,name",
            fields="files(id,name,mimeType,size,modifiedTime)"
        ).execute()
        return jsonify({'files': results.get('files', [])})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/drive/folders')
def list_folders():
    try:
        token = get_token()
        if not token: return jsonify({'error': 'No token'}), 401
        folder_id = request.args.get('folder_id', 'root')
        service = get_drive_service(token)
        results = service.files().list(
            q=f"'{folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
            pageSize=50, orderBy="name",
            fields="files(id,name,mimeType)"
        ).execute()
        return jsonify({'folders': results.get('files', [])})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/drive/search')
def search_files():
    try:
        token = get_token()
        if not token: return jsonify({'error': 'No token'}), 401
        query = request.args.get('q', '')
        service = get_drive_service(token)
        results = service.files().list(
            q=f"name contains '{query}' and trashed=false",
            pageSize=20, fields="files(id,name,mimeType,size,modifiedTime)"
        ).execute()
        return jsonify({'files': results.get('files', [])})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/drive/upload', methods=['POST'])
def upload_file():
    try:
        token = get_token()
        if not token: return jsonify({'error': 'No token'}), 401
        if 'file' not in request.files:
            return jsonify({'error': 'No se envió archivo'}), 400
        file = request.files['file']
        folder_id = request.form.get('folder_id', 'root')
        temp_path = os.path.join(tempfile.gettempdir(), f"nexus_{uuid.uuid4()}_{file.filename}")
        file.save(temp_path)
        service = get_drive_service(token)
        media = MediaFileUpload(temp_path, resumable=False)
        uploaded = service.files().create(
            body={'name': file.filename, 'parents': [folder_id]},
            media_body=media, fields='id,name'
        ).execute()
        try: media._fd.close()
        except: pass
        os.remove(temp_path)
        return jsonify({'success': True, 'file': uploaded})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/drive/folder', methods=['POST'])
def create_folder():
    try:
        token = get_token()
        if not token: return jsonify({'error': 'No token'}), 401
        data = request.json
        service = get_drive_service(token)
        folder = service.files().create(
            body={'name': data.get('name','Nueva Carpeta'),
                  'mimeType': 'application/vnd.google-apps.folder',
                  'parents': [data.get('parent_id','root')]},
            fields='id,name'
        ).execute()
        return jsonify({'success': True, 'folder': folder})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/health')
def health():
    return jsonify({'status': 'NEXUS online'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"\n⚡ NEXUS Backend iniciando en puerto {port}...")
    app.run(host='0.0.0.0', port=port, debug=False)
