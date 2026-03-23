from flask import Flask, request, jsonify, redirect, session
from flask_cors import CORS
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import os, json, tempfile, uuid, requests
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'nexus-secret-2077')
CORS(app, origins="*", supports_credentials=True)

SCOPES = ['https://www.googleapis.com/auth/drive']
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')
GROQ_URL = 'https://api.groq.com/openai/v1/chat/completions'

def get_client_config():
    config_str = os.environ.get('GOOGLE_CREDENTIALS')
    if config_str:
        return json.loads(config_str)
    with open('credentials.json') as f:
        return json.load(f)

def get_drive_service(token_json):
    creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build('drive', 'v3', credentials=creds)

def get_token():
    return request.headers.get('X-Drive-Token') or request.args.get('token')

# ── HEALTH ──
@app.route('/health')
def health():
    return jsonify({'status': 'NEXUS online', 'groq': bool(GROQ_API_KEY)})

# ── CHAT ──
@app.route('/chat', methods=['POST'])
def chat():
    try:
        data = request.json
        messages = data.get('messages', [])
        temperature = data.get('temperature', 0.85)
        max_tokens = data.get('max_tokens', 1500)

        if not GROQ_API_KEY:
            return jsonify({'error': 'GROQ_API_KEY no configurada'}), 500

        # Use fastest model by default, vision model if images present
        has_images = any(
            isinstance(m.get('content'), list) and
            any(c.get('type') == 'image_url' for c in m['content'])
            for m in messages
        )

        model = 'meta-llama/llama-4-scout-17b-16e-instruct' if has_images else 'llama-3.3-70b-versatile'

        res = requests.post(GROQ_URL, headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {GROQ_API_KEY}'
        }, json={
            'model': model,
            'messages': messages,
            'max_tokens': max_tokens,
            'temperature': temperature
        }, timeout=30)

        return jsonify(res.json()), res.status_code

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── AUTH GOOGLE DRIVE ──
@app.route('/auth/login')
def auth_login():
    try:
        config = get_client_config()
        redirect_uri = os.environ.get('REDIRECT_URI', 'https://nexus-backend-ykn2.onrender.com/auth/callback')
        flow = Flow.from_client_config(config, scopes=SCOPES, redirect_uri=redirect_uri)
        auth_url, state = flow.authorization_url(
            access_type='offline',
            prompt='consent'
        )
        # Store state in URL so frontend can pass it back
        return jsonify({'auth_url': auth_url, 'state': state})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/auth/callback')
def auth_callback():
    try:
        import urllib.parse
        config = get_client_config()
        redirect_uri = os.environ.get('REDIRECT_URI', 'https://nexus-backend-ykn2.onrender.com/auth/callback')
        
        # Get code and state from Google's redirect
        code = request.args.get('code')
        state = request.args.get('state')
        
        flow = Flow.from_client_config(
            config, scopes=SCOPES,
            redirect_uri=redirect_uri,
            state=state
        )
        
        # Build the full authorization response URL
        auth_response = request.url
        if auth_response.startswith('http://'):
            auth_response = 'https://' + auth_response[7:]
        
        # Disable PKCE verifier check
        flow.code_verifier = None
        flow.fetch_token(
            authorization_response=auth_response,
            include_client_id=True
        )
        creds = flow.credentials
        token_json = creds.to_json()
        frontend = os.environ.get('FRONTEND_URL', 'https://asmael-chan.github.io/nexus-app')
        return redirect(f"{frontend}?drive_token={urllib.parse.quote(token_json)}")
    except Exception as e:
        return f"<h2>Error: {str(e)}</h2><p>Intenta de nuevo</p><a href='javascript:history.back()'>Volver</a>", 500

# ── DRIVE ──
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
            body={'name': data.get('name', 'Nueva Carpeta'),
                  'mimeType': 'application/vnd.google-apps.folder',
                  'parents': [data.get('parent_id', 'root')]},
            fields='id,name'
        ).execute()
        return jsonify({'success': True, 'folder': folder})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"\n⚡ NEXUS Backend v3.2 en puerto {port}")
    print(f"🤖 Groq: {'✅' if GROQ_API_KEY else '❌ falta GROQ_API_KEY'}")
    app.run(host='0.0.0.0', port=port, debug=False)
