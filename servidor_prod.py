from flask import Flask, request, jsonify, redirect, session
from flask_cors import CORS
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import os, json, tempfile, uuid, requests, re, html, time, sqlite3
from urllib.parse import urlparse, parse_qs, unquote
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'nexus-secret-2077')
CORS(app, origins="*", supports_credentials=True)

SCOPES = ['https://www.googleapis.com/auth/drive']
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')
GROQ_URL = 'https://api.groq.com/openai/v1/chat/completions'
SERPER_API_KEY = os.environ.get('SERPER_API_KEY', '')
PDF_CHAR_LIMIT = int(os.environ.get('PDF_CHAR_LIMIT', '18000'))
PDF_MAX_PAGES = int(os.environ.get('PDF_MAX_PAGES', '12'))
FRONTEND_URL = os.environ.get('FRONTEND_URL', 'https://asmael-chan.github.io/nexus-app')
BACKEND_PUBLIC_URL = os.environ.get('BACKEND_PUBLIC_URL', os.environ.get('RENDER_EXTERNAL_URL', 'https://nexus-backend-ykn2.onrender.com'))
DB_PATH = os.environ.get('BILLING_DB_PATH', os.path.join(os.path.dirname(__file__), 'nexus_billing.sqlite3'))
PAYPAL_PLAN_PRO_URL = os.environ.get('PAYPAL_PLAN_PRO_URL', '')
PAYPAL_PLAN_BUSINESS_URL = os.environ.get('PAYPAL_PLAN_BUSINESS_URL', '')
PAYPAL_ME_URL = os.environ.get('PAYPAL_ME_URL', 'https://www.paypal.me/asmael273')

def env_int(name, default_value):
    try:
        return int(os.environ.get(name, str(default_value)))
    except Exception:
        return default_value

PLAN_CATALOG = {
    'free': {
        'code': 'free',
        'name': 'FREE',
        'amount_cents': 0,
        'currency': 'USD',
        'display_price': os.environ.get('BILLING_FREE_DISPLAY', '$0 USD'),
        'description': 'Entrada gratis para probar NEXUS.'
    },
    'pro': {
        'code': 'pro',
        'name': 'PRO',
        'amount_cents': env_int('BILLING_PRO_AMOUNT_CENTS', 1200),
        'currency': 'USD',
        'display_price': os.environ.get('BILLING_PRO_DISPLAY', '$12 USD'),
        'description': 'Plan mensual individual con mejor precision y web en vivo.'
    },
    'business': {
        'code': 'business',
        'name': 'BUSINESS',
        'amount_cents': env_int('BILLING_BUSINESS_AMOUNT_CENTS', 3900),
        'currency': 'USD',
        'display_price': os.environ.get('BILLING_BUSINESS_DISPLAY', '$39 USD'),
        'description': 'Plan mensual premium para equipos, clientes y uso avanzado.'
    }
}

PAYMENT_METHODS = {
    'paypal': {'code': 'paypal', 'label': 'PayPal'}
}

def utc_now_iso():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_id TEXT NOT NULL UNIQUE,
                customer_name TEXT,
                customer_email TEXT,
                plan_code TEXT NOT NULL,
                provider TEXT NOT NULL,
                payment_method TEXT NOT NULL,
                status TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                currency TEXT NOT NULL,
                checkout_url TEXT,
                external_reference TEXT,
                external_id TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            '''
        )
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS payment_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                event_type TEXT NOT NULL,
                external_id TEXT,
                reference TEXT,
                status TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            '''
        )
        conn.execute('CREATE INDEX IF NOT EXISTS idx_subscriptions_public_id ON subscriptions(public_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_subscriptions_external_reference ON subscriptions(external_reference)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_payment_events_external_id ON payment_events(external_id)')
        conn.commit()

def row_to_subscription(row):
    if not row:
        return None
    item = dict(row)
    metadata = {}
    if item.get('metadata_json'):
        try:
            metadata = json.loads(item['metadata_json'])
        except Exception:
            metadata = {}
    item['metadata'] = metadata
    item.pop('metadata_json', None)
    return item

def get_plan(plan_code):
    return PLAN_CATALOG.get((plan_code or '').strip().lower())

def normalize_payment_method(payment_method):
    value = (payment_method or '').strip().lower()
    return value if value in PAYMENT_METHODS else ''

def create_subscription_record(plan, payment_method, customer_name='', customer_email=''):
    public_id = f"sub_{uuid.uuid4().hex[:14]}"
    provider = 'paypal'
    created_at = utc_now_iso()
    metadata = {
        'plan_display_price': plan['display_price'],
        'payment_method_label': PAYMENT_METHODS.get(payment_method, {}).get('label', payment_method.upper())
    }
    with get_db() as conn:
        conn.execute(
            '''
            INSERT INTO subscriptions (
                public_id, customer_name, customer_email, plan_code, provider, payment_method,
                status, amount_cents, currency, checkout_url, external_reference, external_id,
                metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                public_id,
                customer_name.strip(),
                customer_email.strip(),
                plan['code'],
                provider,
                payment_method,
                'pending' if plan['amount_cents'] > 0 else 'active',
                plan['amount_cents'],
                plan['currency'],
                '',
                public_id,
                '',
                json.dumps(metadata, ensure_ascii=False),
                created_at,
                created_at
            )
        )
        conn.commit()
        row = conn.execute('SELECT * FROM subscriptions WHERE public_id = ?', (public_id,)).fetchone()
    return row_to_subscription(row)

def update_subscription(public_id, **fields):
    if not fields:
        with get_db() as conn:
            row = conn.execute('SELECT * FROM subscriptions WHERE public_id = ?', (public_id,)).fetchone()
            return row_to_subscription(row)

    allowed = {
        'customer_name', 'customer_email', 'provider', 'payment_method', 'status',
        'amount_cents', 'currency', 'checkout_url', 'external_reference', 'external_id',
        'metadata_json', 'updated_at'
    }
    payload = {key: value for key, value in fields.items() if key in allowed}
    payload['updated_at'] = utc_now_iso()
    set_clause = ', '.join(f"{key} = ?" for key in payload)
    values = list(payload.values()) + [public_id]

    with get_db() as conn:
        conn.execute(f'UPDATE subscriptions SET {set_clause} WHERE public_id = ?', values)
        conn.commit()
        row = conn.execute('SELECT * FROM subscriptions WHERE public_id = ?', (public_id,)).fetchone()
    return row_to_subscription(row)

def update_subscription_by_reference(reference, **fields):
    if not reference:
        return None
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM subscriptions WHERE external_reference = ? OR public_id = ? ORDER BY id DESC LIMIT 1',
            (reference, reference)
        ).fetchone()
    if not row:
        return None
    return update_subscription(row['public_id'], **fields)

def log_payment_event(provider, event_type, payload, external_id='', reference='', status='received'):
    with get_db() as conn:
        conn.execute(
            '''
            INSERT INTO payment_events (provider, event_type, external_id, reference, status, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                provider,
                event_type,
                external_id or '',
                reference or '',
                status or '',
                json.dumps(payload, ensure_ascii=False),
                utc_now_iso()
            )
        )
        conn.commit()

def build_paypal_checkout_url(plan_code):
    plan_code = (plan_code or '').lower()
    if plan_code == 'pro' and PAYPAL_PLAN_PRO_URL:
        return PAYPAL_PLAN_PRO_URL
    if plan_code == 'business' and PAYPAL_PLAN_BUSINESS_URL:
        return PAYPAL_PLAN_BUSINESS_URL
    if PAYPAL_ME_URL and plan_code in {'pro', 'business'}:
        plan = get_plan(plan_code)
        amount_cents = int((plan or {}).get('amount_cents', 0) or 0)
        currency = ((plan or {}).get('currency') or 'USD').upper()
        if amount_cents > 0:
            amount_value = f"{amount_cents / 100:.2f}".rstrip('0').rstrip('.')
            return f"{PAYPAL_ME_URL.rstrip('/')}/{amount_value}{currency}"
        return PAYPAL_ME_URL
    return ''

def get_billing_config():
    paypal_ready = bool(PAYPAL_PLAN_PRO_URL or PAYPAL_PLAN_BUSINESS_URL or PAYPAL_ME_URL)
    return {
        'plans': list(PLAN_CATALOG.values()),
        'methods': [
            {'code': 'paypal', 'label': 'PayPal', 'enabled': paypal_ready, 'provider': 'paypal'}
        ],
        'targets': {
            'paypal_ready': paypal_ready,
            'database_path': DB_PATH
        }
    }

def normalize_subscription_status(raw_status):
    value = (raw_status or '').strip().lower()
    if value in {'approved', 'active', 'paid', 'completed'}:
        return 'active'
    if value in {'declined', 'failed', 'error', 'denied'}:
        return 'failed'
    if value in {'voided', 'cancelled', 'canceled'}:
        return 'cancelled'
    if value in {'pending', 'processing', 'checkout_created'}:
        return 'pending'
    return value or 'pending'

init_db()

def get_client_config():
    config_str = os.environ.get('GOOGLE_CREDENTIALS')
    if config_str:
        return json.loads(config_str)
    with open('credentials.json') as f:
        return json.load(f)

def get_drive_service(token_json):
    try:
        token_data = json.loads(token_json)
        # Handle both formats: Google Credentials JSON and raw token response
        if 'access_token' in token_data and 'token' not in token_data:
            # Raw token from our manual exchange
            creds = Credentials(
                token=token_data['access_token'],
                refresh_token=token_data.get('refresh_token'),
                token_uri='https://oauth2.googleapis.com/token',
                client_id=get_client_config().get('web', get_client_config().get('installed', {}))['client_id'],
                client_secret=get_client_config().get('web', get_client_config().get('installed', {}))['client_secret'],
                scopes=SCOPES
            )
        else:
            creds = Credentials.from_authorized_user_info(token_data, SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        raise Exception(f"Error con el token de Drive: {str(e)}")

def get_token():
    return request.headers.get('X-Drive-Token') or request.args.get('token')

def extract_text_from_content(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get('type') == 'text':
                text = (item.get('text') or '').strip()
                if text:
                    parts.append(text)
        return '\n'.join(parts).strip()
    return ''

def get_latest_user_query(messages):
    for message in reversed(messages or []):
        if message.get('role') != 'user':
            continue
        text = extract_text_from_content(message.get('content'))
        if text:
            return re.sub(r'\s+', ' ', text)[:280]
    return ''

def clean_text(value):
    if not value:
        return ''
    without_tags = re.sub(r'<[^>]+>', ' ', value)
    return re.sub(r'\s+', ' ', html.unescape(without_tags)).strip()

def normalize_result_url(url):
    if not url:
        return ''
    if url.startswith('//'):
        url = 'https:' + url
    if 'duckduckgo.com/l/?' in url:
        redirected = parse_qs(urlparse(url).query).get('uddg', [''])[0]
        if redirected:
            return unquote(redirected)
    return url

def search_web_serper(query, max_results=5):
    response = requests.post(
        'https://google.serper.dev/search',
        headers={
            'X-API-KEY': SERPER_API_KEY,
            'Content-Type': 'application/json'
        },
        json={'q': query, 'num': max_results},
        timeout=20
    )
    response.raise_for_status()
    data = response.json()
    results = []
    for item in data.get('organic', [])[:max_results]:
        title = (item.get('title') or '').strip()
        url = (item.get('link') or '').strip()
        snippet = (item.get('snippet') or '').strip()
        if not title or not url:
            continue
        results.append({
            'title': title,
            'url': url,
            'snippet': snippet,
            'source': 'Google'
        })
    return results

def search_web_duckduckgo(query, max_results=5):
    from bs4 import BeautifulSoup

    response = requests.post(
        'https://html.duckduckgo.com/html/',
        data={'q': query},
        headers={'User-Agent': 'Mozilla/5.0'},
        timeout=20
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    results = []

    for result in soup.select('.result'):
        link = result.select_one('a.result__a')
        snippet_node = result.select_one('.result__snippet')
        if not link:
            continue

        url = normalize_result_url(link.get('href', ''))
        title = clean_text(link.get_text(' ', strip=True))
        snippet = clean_text(snippet_node.get_text(' ', strip=True) if snippet_node else '')

        if not title or not url:
            continue

        results.append({
            'title': title,
            'url': url,
            'snippet': snippet,
            'source': 'DuckDuckGo'
        })
        if len(results) >= max_results:
            break

    return results

def run_web_search(query, max_results=5):
    if not query:
        return []

    if SERPER_API_KEY:
        try:
            results = search_web_serper(query, max_results)
            if results:
                return results
        except Exception:
            pass

    try:
        return search_web_duckduckgo(query, max_results)
    except Exception:
        return []

def build_web_context(query, results):
    if not results:
        return ''

    blocks = [f'Contexto web reciente para la consulta: "{query}"']
    for idx, item in enumerate(results, start=1):
        blocks.append(
            f"[{idx}] {item.get('title', 'Fuente')}\n"
            f"URL: {item.get('url', '')}\n"
            f"Resumen: {item.get('snippet', 'Sin resumen disponible.')}"
        )
    return '\n\n'.join(blocks)

# ── HEALTH ──
@app.route('/health')
def health():
    return jsonify({
        'status': 'NEXUS online',
        'groq': bool(GROQ_API_KEY),
        'web_provider': 'google-serper' if SERPER_API_KEY else 'duckduckgo-fallback',
        'billing_db': DB_PATH,
        'paypal_ready': bool(PAYPAL_PLAN_PRO_URL or PAYPAL_PLAN_BUSINESS_URL or PAYPAL_ME_URL)
    })

# ── CHAT ──
# BILLING
@app.route('/billing/config')
def billing_config():
    return jsonify(get_billing_config())

@app.route('/billing/subscriptions/<public_id>')
def billing_subscription_detail(public_id):
    with get_db() as conn:
        row = conn.execute('SELECT * FROM subscriptions WHERE public_id = ?', (public_id,)).fetchone()
    if not row:
        return jsonify({'error': 'Suscripcion no encontrada'}), 404
    return jsonify({'subscription': row_to_subscription(row)})

@app.route('/billing/checkout', methods=['POST'])
def billing_checkout():
    try:
        payload = request.json or {}
        plan = get_plan(payload.get('plan'))
        payment_method = normalize_payment_method(payload.get('payment_method'))
        override_url = (payload.get('override_url') or '').strip()
        customer_name = (payload.get('customer_name') or '').strip()
        customer_email = (payload.get('customer_email') or '').strip()

        if not plan:
            return jsonify({'error': 'Plan invalido'}), 400
        if not payment_method:
            return jsonify({'error': 'Metodo de pago invalido'}), 400

        subscription = create_subscription_record(plan, payment_method, customer_name, customer_email)

        if plan['amount_cents'] <= 0:
            subscription = update_subscription(subscription['public_id'], status='active')
            return jsonify({
                'ok': True,
                'subscription': subscription,
                'checkout': {'type': 'free', 'message': 'Plan free activado'}
            })

        checkout = None

        if payment_method == 'paypal':
            direct_url = override_url if re.match(r'^https?://', override_url) else build_paypal_checkout_url(plan['code'])
            if direct_url:
                subscription = update_subscription(subscription['public_id'], checkout_url=direct_url, status='checkout_created')
                checkout = {'type': 'redirect', 'provider': 'paypal', 'url': direct_url}

        if not checkout:
            subscription = update_subscription(subscription['public_id'], status='config_pending')
            return jsonify({
                'ok': False,
                'needs_configuration': True,
                'message': (
                    'Faltan credenciales o enlaces publicos para completar este metodo. '
                    'Configura PayPal o revisa el enlace publico del checkout y vuelve a intentarlo.'
                ),
                'subscription': subscription,
                'billing': get_billing_config()
            }), 503

        return jsonify({
            'ok': True,
            'subscription': subscription,
            'checkout': checkout,
            'billing': get_billing_config()
        })
    except requests.HTTPError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text if e.response is not None else str(e)
        return jsonify({'error': 'Error creando checkout', 'detail': detail}), 502
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/billing/return')
def billing_return():
    subscription_id = (request.args.get('subscription_id') or '').strip()
    provider = (request.args.get('provider') or '').strip().lower()
    transaction_id = (
        request.args.get('id')
        or request.args.get('transaction_id')
        or request.args.get('subscription')
        or ''
    ).strip()

    if subscription_id:
        updates = {'status': 'pending'}
        if transaction_id:
            updates['external_id'] = transaction_id
        update_subscription(subscription_id, **updates)

    import urllib.parse
    params = {
        'billing_subscription': subscription_id,
        'billing_provider': provider or 'checkout',
        'billing_status': 'pending'
    }
    if transaction_id:
        params['billing_transaction_id'] = transaction_id

    return redirect(f"{FRONTEND_URL}?{urllib.parse.urlencode(params)}")

@app.route('/billing/webhook/paypal', methods=['POST'])
def billing_paypal_webhook():
    payload = request.json or {}
    event_type = payload.get('event_type') or 'paypal.webhook'
    resource = payload.get('resource') or {}
    reference = resource.get('custom_id') or resource.get('invoice_id') or ''
    external_id = resource.get('id') or ''
    status = normalize_subscription_status(resource.get('status') or event_type)

    log_payment_event('paypal', event_type, payload, external_id=external_id, reference=reference, status=status)
    if reference:
        update_subscription_by_reference(reference, status=status, external_id=external_id)
    return jsonify({'received': True})

@app.route('/chat', methods=['POST'])
def chat():
    try:
        data = request.json
        messages = data.get('messages', [])
        temperature = data.get('temperature', 0.85)
        max_tokens = data.get('max_tokens', 1500)
        use_web = bool(data.get('use_web', False))
        precision_mode = bool(data.get('precision_mode', True))
        reply_language = (data.get('reply_language') or 'auto').strip()
        web_query = ''
        web_results = []

        if not GROQ_API_KEY:
            return jsonify({'error': 'GROQ_API_KEY no configurada'}), 500

        # Use fastest model by default, vision model if images present
        has_images = any(
            isinstance(m.get('content'), list) and
            any(c.get('type') == 'image_url' for c in m['content'])
            for m in messages
        )

        model = 'meta-llama/llama-4-scout-17b-16e-instruct' if has_images else 'llama-3.3-70b-versatile'
        outbound_messages = list(messages)
        injected_messages = []

        if reply_language.lower() == 'auto':
            injected_messages.append({
                'role': 'system',
                'content': (
                    'Responde en el mismo idioma que use el usuario en su ultimo mensaje. '
                    'Si mezcla idiomas, prioriza el idioma principal y manten nombres propios, codigo y citas sin traducir salvo que lo pidan.'
                )
            })
        else:
            injected_messages.append({
                'role': 'system',
                'content': (
                    f'Responde en {reply_language}. '
                    'Mantiene nombres propios, codigo y citas en su forma original salvo que el usuario pida traducirlos.'
                )
            })

        if precision_mode:
            injected_messages.append({
                'role': 'system',
                'content': (
                    'Prioriza exactitud, claridad y estructura. '
                    'Si la pregunta trata de personajes, obras, marcas, historia, ciencia o temas especificos, '
                    'responde con datos concretos, no inventes informacion y distingue entre hechos, contexto e inferencias. '
                    'Antes de responder sobre una persona, personaje, obra o franquicia, identifica con exactitud el nombre, la obra y el contexto; '
                    'si hay ambiguedad o nombres parecidos, pide una aclaracion breve en vez de asumir o cambiarlo por otro. '
                    'Nunca sustituyas un personaje por otro mas conocido solo porque el nombre se parece. '
                    'Si no puedes identificarlo con seguridad, dilo primero y luego pide precision.'
                )
            })

        if use_web:
            web_query = get_latest_user_query(messages)
            web_results = run_web_search(web_query, 5)
            if web_results:
                injected_messages.append({
                    'role': 'system',
                    'content': (
                        'Usa el siguiente contexto web reciente para enriquecer la respuesta. '
                        'Cuando una afirmacion dependa de estas fuentes, cita con [1], [2], etc. '
                        'Si el contexto no alcanza, dilo sin inventar.\n\n'
                        + build_web_context(web_query, web_results)
                    )
                })
            else:
                injected_messages.append({
                    'role': 'system',
                    'content': (
                        'El modo web esta activo, pero no se recuperaron resultados recientes. '
                        'No inventes fuentes ni cites informacion no verificada.'
                    )
                })

        if injected_messages:
            if messages and messages[0].get('role') == 'system':
                outbound_messages = [messages[0]] + injected_messages + messages[1:]
            else:
                outbound_messages = injected_messages + messages

        res = requests.post(GROQ_URL, headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {GROQ_API_KEY}'
        }, json={
            'model': model,
            'messages': outbound_messages,
            'max_tokens': max_tokens,
            'temperature': temperature
        }, timeout=30)

        payload = res.json()
        if isinstance(payload, dict):
            payload['nexus_meta'] = {
                'model': model,
                'precision_mode': precision_mode,
                'reply_language': reply_language,
                'web_used': bool(web_results),
                'web_query': web_query,
                'web_results': web_results
            }

        return jsonify(payload), res.status_code

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── AUTH GOOGLE DRIVE ──
@app.route('/web/search')
def web_search():
    try:
        query = (request.args.get('q') or '').strip()
        limit = max(1, min(int(request.args.get('limit', 5)), 10))
        if not query:
            return jsonify({'error': 'Consulta vacia'}), 400

        results = run_web_search(query, limit)
        return jsonify({
            'query': query,
            'results': results,
            'provider': 'google-serper' if SERPER_API_KEY else 'duckduckgo-fallback'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/auth/login')
def auth_login():
    try:
        config = get_client_config()
        client_info = config.get('web', config.get('installed', {}))
        client_id = client_info['client_id']
        redirect_uri = os.environ.get('REDIRECT_URI', 'https://nexus-backend-ykn2.onrender.com/auth/callback')
        
        import urllib.parse, secrets
        state = secrets.token_urlsafe(16)
        
        params = {
            'client_id': client_id,
            'redirect_uri': redirect_uri,
            'response_type': 'code',
            'scope': 'https://www.googleapis.com/auth/drive',
            'access_type': 'offline',
            'prompt': 'consent',
            'state': state
        }
        
        auth_url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urllib.parse.urlencode(params)
        return jsonify({'auth_url': auth_url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/auth/callback')
def auth_callback():
    try:
        import urllib.parse
        config = get_client_config()
        client_info = config.get('web', config.get('installed', {}))
        client_id = client_info['client_id']
        client_secret = client_info['client_secret']
        redirect_uri = os.environ.get('REDIRECT_URI', 'https://nexus-backend-ykn2.onrender.com/auth/callback')
        
        code = request.args.get('code')
        if not code:
            return f"<h2>Error: no se recibió código de autorización</h2>", 400
        
        # Exchange code for token manually
        token_res = requests.post('https://oauth2.googleapis.com/token', data={
            'code': code,
            'client_id': client_id,
            'client_secret': client_secret,
            'redirect_uri': redirect_uri,
            'grant_type': 'authorization_code'
        })
        
        token_data = token_res.json()
        
        if 'error' in token_data:
            return f"<h2>Error: {token_data['error']}</h2><p>{token_data.get('error_description','')}</p>", 400
        
        frontend = os.environ.get('FRONTEND_URL', 'https://asmael-chan.github.io/nexus-app')
        return redirect(f"{frontend}?drive_token={urllib.parse.quote(json.dumps(token_data))}")
    except Exception as e:
        return f"<h2>Error: {str(e)}</h2><a href='javascript:history.back()'>Volver</a>", 500

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

        if folder_id == 'root':
            # Get both owned and shared folders
            results = service.files().list(
                q="mimeType='application/vnd.google-apps.folder' and trashed=false and (('root' in parents) or sharedWithMe=true)",
                pageSize=50, orderBy="name",
                fields="files(id,name,mimeType,shared)"
            ).execute()
        else:
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

# ── EXTRACT PDF TEXT ──
@app.route('/extract/pdf', methods=['POST'])
def extract_pdf():
    try:
        import PyPDF2
        if 'file' not in request.files:
            return jsonify({'error': 'No se envió archivo'}), 400
        started_at = time.time()
        file = request.files['file']
        max_chars = max(4000, min(int(request.form.get('max_chars', PDF_CHAR_LIMIT)), 40000))
        max_pages = max(1, min(int(request.form.get('max_pages', PDF_MAX_PAGES)), 40))
        reader = PyPDF2.PdfReader(file, strict=False)
        total_pages = len(reader.pages)
        parts = []
        total_chars = 0
        pages_scanned = 0
        for idx, page in enumerate(reader.pages):
            if idx >= max_pages or total_chars >= max_chars:
                break
            pages_scanned = idx + 1
            page_text = page.extract_text() or ''
            if not page_text:
                continue
            remaining = max_chars - total_chars
            if remaining <= 0:
                break
            snippet = page_text[:remaining]
            parts.append(snippet)
            total_chars += len(snippet)
        text = ''.join(parts).strip()
        if not text.strip():
            return jsonify({'error': 'No se pudo extraer texto del PDF'}), 400
        return jsonify({
            'text': text,
            'pages_scanned': pages_scanned,
            'total_pages': total_pages,
            'truncated': pages_scanned < total_pages or total_chars >= max_chars,
            'elapsed_ms': int((time.time() - started_at) * 1000),
            'max_chars': max_chars
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"\n⚡ NEXUS Backend v3.2 en puerto {port}")
    print(f"🤖 Groq: {'✅' if GROQ_API_KEY else '❌ falta GROQ_API_KEY'}")
    app.run(host='0.0.0.0', port=port, debug=False)
