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

# ───────── CONFIG ─────────

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

# ───────── HEALTH ─────────
@app.route('/health')
def health():
    return jsonify({'status': 'NEXUS GOD MODE', 'groq': bool(GROQ_API_KEY)})

# ───────── CHAT IA ─────────
@app.route('/chat', methods=['POST'])
def chat():
    try:
        data = request.json
        messages = data.get('messages', [])

        res = requests.post(GROQ_URL, headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {GROQ_API_KEY}'
        }, json={
            'model': 'llama-3.3-70b-versatile',
            'messages': messages
        })

        return jsonify(res.json()), res.status_code

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ───────── AUTH GOOGLE (FIX PKCE) ─────────
@app.route('/auth/login')
def auth_login():
    try:
        config = get_client_config()
        redirect_uri = os.environ.get('REDIRECT_URI')

        flow = Flow.from_client_config(config, scopes=SCOPES, redirect_uri=redirect_uri)

        auth_url, state = flow.authorization_url(
            access_type='offline',
            prompt='consent'
        )

        # 🔥 GUARDAR SESIÓN
        session['state'] = state
        session['code_verifier'] = flow.code_verifier

        return jsonify({'auth_url': auth_url})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/auth/callback')
def auth_callback():
    try:
        import urllib.parse

        config = get_client_config()
        redirect_uri = os.environ.get('REDIRECT_URI')

        flow = Flow.from_client_config(
            config,
            scopes=SCOPES,
            redirect_uri=redirect_uri,
            state=session.get('state')
        )

        # 🔥 RESTAURAR PKCE
        flow.code_verifier = session.get('code_verifier')

        flow.fetch_token(authorization_response=request.url)

        creds = flow.credentials
        token_json = creds.to_json()

        frontend = os.environ.get('FRONTEND_URL')

        return redirect(f"{frontend}?drive_token={urllib.parse.quote(token_json)}")

    except Exception as e:
        return f"<h2>Error: {str(e)}</h2>", 500

# ───────── DRIVE ─────────
@app.route('/drive/list')
def list_files():
    try:
        token = get_token()
        if not token:
            return jsonify({'error': 'No token'}), 401

        service = get_drive_service(token)
        results = service.files().list(
            q="trashed=false",
            pageSize=50,
            fields="files(id,name,mimeType,size)"
        ).execute()

        return jsonify({'files': results.get('files', [])})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# 📂 CREAR CARPETA
@app.route('/drive/folder', methods=['POST'])
def create_folder():
    try:
        token = get_token()
        data = request.json

        service = get_drive_service(token)
        folder = service.files().create(
            body={
                'name': data.get('name'),
                'mimeType': 'application/vnd.google-apps.folder'
            }
        ).execute()

        return jsonify(folder)

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# 📤 SUBIR ARCHIVO
@app.route('/drive/upload', methods=['POST'])
def upload_file():
    try:
        token = get_token()

        file = request.files['file']
        temp_path = os.path.join(tempfile.gettempdir(), file.filename)
        file.save(temp_path)

        service = get_drive_service(token)

        media = MediaFileUpload(temp_path)
        uploaded = service.files().create(
            body={'name': file.filename},
            media_body=media
        ).execute()

        os.remove(temp_path)

        return jsonify(uploaded)

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# 🔍 BUSCAR
@app.route('/drive/search')
def search():
    try:
        token = get_token()
        q = request.args.get('q')

        service = get_drive_service(token)
        results = service.files().list(
            q=f"name contains '{q}'",
            fields="files(id,name)"
        ).execute()

        return jsonify(results)

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# 👁️ PREVIEW (GOD MODE)
@app.route('/drive/preview')
def preview():
    try:
        file_id = request.args.get('file_id')
        return jsonify({
            'preview': f"https://drive.google.com/file/d/{file_id}/preview",
            'download': f"https://drive.google.com/uc?id={file_id}&export=download"
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# 🧠 INFO ARCHIVO
@app.route('/drive/info')
def file_info():
    try:
        token = get_token()
        file_id = request.args.get('file_id')

        service = get_drive_service(token)
        file = service.files().get(fileId=file_id).execute()

        return jsonify(file)

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ❌ ELIMINAR
@app.route('/drive/delete', methods=['POST'])
def delete():
    try:
        token = get_token()
        file_id = request.json.get('file_id')

        service = get_drive_service(token)
        service.files().delete(fileId=file_id).execute()

        return jsonify({'deleted': True})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ───────── RUN ─────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"⚡ NEXUS GOD MODE en {port}")
    app.run(host='0.0.0.0', port=port)
