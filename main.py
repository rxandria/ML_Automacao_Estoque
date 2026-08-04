# -*- coding: utf-8 -*-
"""
Main pipeline orchestrator and Local Web Server for the Mercado Livre Automation project.
Supports background removal and multiple image upload.
"""
import os
import sys
import json
import re
import datetime
from datetime import timezone, timedelta
try:
    from zoneinfo import ZoneInfo
    BRASILIA_TZ = ZoneInfo("America/Sao_Paulo")
except Exception:
    BRASILIA_TZ = timezone(timedelta(hours=-3))

def get_brasilia_time():
    """
    Retorna o objeto datetime atual no fuso horário oficial de Brasília (America/Sao_Paulo, UTC-3).
    """
    return datetime.datetime.now(timezone.utc).astimezone(BRASILIA_TZ)

def format_brasilia_time(fmt="%d/%m/%Y %H:%M:%S"):
    """
    Retorna a data e hora atual formatada no fuso horário de Brasília (padrão DD/MM/YYYY HH:MM:SS).
    """
    return get_brasilia_time().strftime(fmt)

import gc

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from scripts.vision_processor import sanitize_title

from scripts.drive_sheets_sync import (
    authenticate, 
    setup_drive_structure, 
    setup_google_sheet, 
    upload_product_photo, 
    add_product_to_sheet,
    delete_drive_file,
    delete_sheet_row,
    get_first_sheet_name,
    format_sheet_range,
    get_sanitized_env,
    safe_sheets_get,
    safe_sheets_update,
    safe_sheets_append,
    HEADERS
)
from scripts.ml_api_publisher import MLPublisher
import uuid

# Contas de acesso padrão (lidas do ambiente ou fallbacks)
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "owner@duotech.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "duotech123")

USER2_USERNAME = os.environ.get("USER2_USERNAME", "colaborador@duotech.com")
USER2_PASSWORD = os.environ.get("USER2_PASSWORD", "duotech456")

# Gerenciamento de sessões com persistência em disco
SESSIONS_FILE = os.path.join("temp_uploads", "sessions.json")

def load_sessions():
    sessions = {}
    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                sessions = json.load(f)
        except Exception as e:
            print(f"⚠️ Erro ao carregar sessões salvas em disco: {e}")
    return sessions

def save_sessions(sessions):
    try:
        os.makedirs("temp_uploads", exist_ok=True)
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(sessions, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ Erro ao salvar sessões em disco: {e}")

SESSIONS = load_sessions()

# Armazenamento local de produtos para resiliência do dashboard quando Google APIs falharem
LOCAL_PRODUCTS_FILE = os.path.join("temp_uploads", "local_products.json")

def load_local_products():
    if os.path.exists(LOCAL_PRODUCTS_FILE):
        try:
            with open(LOCAL_PRODUCTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Erro ao ler produtos salvos em disco: {e}")
    return []

def save_local_products(products):
    try:
        os.makedirs("temp_uploads", exist_ok=True)
        with open(LOCAL_PRODUCTS_FILE, "w", encoding="utf-8") as f:
            json.dump(products, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ Erro ao salvar produtos em disco: {e}")

LOCAL_PRODUCTS = load_local_products()

def cleanup_temp_files(file_paths):
    """
    Exclui arquivos temporários de upload da pasta temp_uploads/ se existirem,
    preservando apenas fixtures e arquivos de estado essenciais.
    """
    protected_files = {
        "test_product.jpg", "test_revisar.jpg", "fone_bluetooth.jpg",
        "sessions.json", "local_products.json"
    }
    if isinstance(file_paths, str):
        file_paths = [file_paths]
    for p in file_paths:
        if not p:
            continue
        try:
            basename = os.path.basename(p)
            if basename not in protected_files and os.path.exists(p):
                os.remove(p)
                print(f"🧹 Arquivo temporário excluído do disco: {p}")
        except Exception as err:
            print(f"⚠️ Erro ao remover arquivo temporário '{p}': {err}")


# ID da pasta principal no Drive (lida de DRIVE_FOLDER_ID com fallback)
PARENT_FOLDER_ID = get_sanitized_env("DRIVE_FOLDER_ID", "1pjqOPcWHW8gCZ9GdLF7ta6NESN0dyw70")


def run_pipeline(image_paths, dry_run=True, product_id=None):
    from scripts.vision_processor import optimize_image_for_ml, analyze_product_image

    # Aceita string única ou lista de caminhos
    if isinstance(image_paths, str):
        image_paths = [image_paths]
        
    print("\n" + "="*60)
    print(f"🎬 INICIANDO PIPELINE: {len(image_paths)} Fotos (dry_run={dry_run})")
    print(f"   Arquivos: {image_paths}")
    print("="*60)
    
    if not product_id:
        product_id = f"MLB{uuid.uuid4().hex[:10].upper()}"

    try:
        # 1. Autenticação com as APIs do Google
        print("\n🔑 Passo 1: Autenticando com Google APIs...")
        creds = authenticate()
        
        # 2. Inicialização das estruturas no Drive e Sheets
        print("\n📁 Passo 2: Configurando estrutura no Google Drive/Sheets...")
        photos_folder_id = setup_drive_structure(PARENT_FOLDER_ID, creds)
        sheet_id = setup_google_sheet(PARENT_FOLDER_ID, creds)
        
        # 3. Análise do produto com a IA (baseado na 1ª imagem / Principal)
        print("\n🧠 Passo 3: Analisando imagem principal do produto...")
        main_image = image_paths[0]
        product_data = analyze_product_image(main_image)
        gc.collect()
        
        print(f"   Confiança da IA: {product_data.get('confidence_score')}")
        print(f"   Revisão Manual Necessária: {product_data.get('requires_manual_review')}")
        
        # 4. Sincronização IMEDIATA no Google Sheets (Garante a gravação antes dos uploads de mídia no Drive)
        requires_review = product_data.get("requires_manual_review", False)
        status = "REVISAO_MANUAL" if requires_review else "HOMOLOGADO (DRY RUN)"
        reason = product_data.get("review_reason", "Aprovado via IA") if requires_review else "Validação do payload aprovada via IA."

        print(f"\n📊 Passo 4: Gravando registro do produto imediatamente no Google Sheets (Status: {status})...")
        sheets_ok = add_product_to_sheet(
            sheet_id=sheet_id,
            product_data=product_data,
            status=status,
            review_needed=requires_review,
            review_reason=reason,
            creds=creds,
            product_id=product_id
        )
        if sheets_ok:
            print("📊 [GOOGLE SHEETS SYNC OK] Google Sheets Sync Concluído com Sucesso (Antes dos Uploads)!")

        # 5. Upload sequencial leve das fotos para o Google Drive com fundo branco puro (#FFFFFF)
        print("\n📷 Passo 5: Otimizando fundo branco (#FFFFFF) e enviando fotos para o Google Drive...")
        public_urls = []
        for idx, img_path in enumerate(image_paths):
            print(f"🚀 Otimizando e enviando foto {idx+1}/{len(image_paths)} para o Google Drive...")
            processed_img = optimize_image_for_ml(img_path, remove_bg=True)
            target_upload_path = processed_img if processed_img and os.path.exists(processed_img) else img_path
            url = upload_product_photo(target_upload_path, photos_folder_id, creds)
            if url:
                public_urls.append(url)
            gc.collect()

        concatenated_urls = ", ".join(public_urls) if public_urls else ""
        product_data["url_fotos"] = concatenated_urls
        drive_thumb = public_urls[0] if public_urls else ""

        # 6. Atualização final do cache local de produtos com as URLs do Google Drive
        local_item = {
            "row_num": len(LOCAL_PRODUCTS) + 2,
            "id": product_id,
            "titulo": product_data.get("titulo", "Produto"),
            "categoria": product_data.get("categoria", "Outros"),
            "preco": product_data.get("preco_sugerido", 50.0),
            "estoque": product_data.get("estoque", 1),
            "condicao": product_data.get("condicao", "used"),
            "url_fotos": concatenated_urls,
            "status": status,
            "review_needed": requires_review,
            "motivo_revisao": reason,
            "date": format_brasilia_time("%d/%m/%Y %H:%M:%S"),
            "local_thumb": drive_thumb or ("/temp_uploads/" + os.path.basename(main_image) if os.path.exists(main_image) else "/temp_uploads/test_product.jpg"),
            "original_filename": os.path.basename(main_image)
        }
        
        found = False
        for idx_p, p in enumerate(LOCAL_PRODUCTS):
            if str(p.get("id")) == str(product_id):
                LOCAL_PRODUCTS[idx_p] = local_item
                found = True
                break
        if not found:
            LOCAL_PRODUCTS.append(local_item)

        save_local_products(LOCAL_PRODUCTS)
        gc.collect()

        if requires_review:
            print("\n❌ Pipeline interrompido: O produto necessita de revisão humana.")
            return {
                "success": False,
                "requires_manual_review": True,
                "reason": reason,
                "product_data": product_data
            }

        # 7. Validação do Mercado Livre (Dry Run)
        print("\n🛍️ Passo 7: Validando publicação no Mercado Livre...")
        publisher = MLPublisher(access_token="mock_token_abc123")
        publisher.publish_item(product_data, public_urls, dry_run=dry_run)
            
        print(f"\n🎉 PIPELINE CONCLUÍDO COM SUCESSO! Status: {status}")
        return {
            "success": True,
            "requires_manual_review": False,
            "status": status,
            "product_data": product_data
        }


        
    except Exception as e:
        import traceback
        print(f"\n❌ Falha grave na execução do pipeline: {e}")
        traceback.print_exc()
        gc.collect()
        return {
            "success": False,
            "requires_manual_review": False,
            "error": str(e)
        }
    finally:
        cleanup_temp_files(image_paths)
        if 'optimized_paths' in locals():
            cleanup_temp_files(optimized_paths)
        gc.collect()




def update_product_in_sheet(sheet_id, row_num, product_data, status, review_needed, review_reason, creds, product_id=""):
    """
    Atualiza uma linha específica na planilha Google Sheets.
    """
    if not creds or sheet_id == "mock_sheet_id":
        print(f"⚠️ [GOOGLE SHEETS] Credenciais nulas ou planilha simulada. Ignorando atualização remota para linha {row_num}")
        return False
    try:
        from googleapiclient.discovery import build
        sheets_service = build("sheets", "v4", credentials=creds)
        now_str = format_brasilia_time("%d/%m/%Y %H:%M:%S")

        
        row_data = [
            product_id,                                              # ID Produto
            product_data.get("titulo", ""),                          # Título
            product_data.get("categoria", ""),                       # Categoria
            product_data.get("preco_sugerido", 0.0),                 # Preço (R$)
            product_data.get("estoque", 1),                          # Estoque
            product_data.get("condicao", "new"),                     # Condição
            product_data.get("url_fotos", ""),                       # URL Fotos
            status,                                                  # Status ML
            "SIM" if review_needed else "NÃO",                        # Revisão Necessária
            review_reason,                                           # Motivo Revisão
            now_str                                                  # Data Criação
        ]
        
        body = {
            'values': [row_data]
        }
        
        sheet_name = get_first_sheet_name(sheets_service, sheet_id)
        safe_sheets_update(
            sheets_service=sheets_service,
            spreadsheet_id=sheet_id,
            sheet_name=sheet_name,
            cell_range=f"A{row_num}:K{row_num}",
            value_input_option="USER_ENTERED",
            body=body
        )
        print(f"📊 Linha {row_num} da planilha atualizada via Revisão Manual!")
        return True
        
    except Exception as e:
        import traceback
        print(f"❌ [GOOGLE SHEETS ERROR] Erro ao atualizar linha na planilha: {e}")
        traceback.print_exc()
        return False


# --- Servidor Local HTTP ---

class DashboardHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        try:
            sys.stdout.write(f"[{format_brasilia_time('%H:%M:%S')}] {format % args}\n")
        except Exception:
            pass

    def safe_write_response(self, status=200, content=b"", content_type='application/json'):
        """
        Envia resposta HTTP com tratamento seguro contra BrokenPipeError, ConnectionResetError e OSError.
        Garante que desconexões bruscas de sockets de clientes móveis nunca interrompam
        a execução do servidor nem das threads de fundo de sincronização/persistência.
        """
        try:
            if isinstance(content, (dict, list)):
                content_bytes = json.dumps(content).encode('utf-8')
            elif isinstance(content, str):
                content_bytes = content.encode('utf-8')
            else:
                content_bytes = content

            self.send_cors_response(status, content_type)
            self.wfile.write(content_bytes)
        except (BrokenPipeError, ConnectionResetError, OSError) as sock_err:
            print(f"⚠️ [SOCKET WARN] Cliente HTTP desconectou antes da entrega da resposta ({sock_err}).")
        except Exception as err:
            print(f"⚠️ [HTTP RESP ERROR] Falha ao enviar resposta HTTP: {err}")



    def get_authenticated_user(self):
        token = None
        
        # 1. Tenta carregar dos cookies
        cookie_header = self.headers.get('Cookie')
        if cookie_header:
            cookies = re.findall(r'session_token=([^;]+)', cookie_header)
            if cookies:
                token = cookies[0].strip()
                
        # 2. Tenta carregar do header Authorization
        if not token:
            auth_header = self.headers.get('Authorization')
            if auth_header and auth_header.startswith('Bearer '):
                token = auth_header.split('Bearer ', 1)[1].strip()

        # 3. Tenta carregar dos parâmetros da URL
        if not token:
            parsed = urlparse(self.path)
            query_params = parse_qs(parsed.query)
            if 'token' in query_params:
                token = query_params['token'][0].strip()

        if token:
            if token in SESSIONS:
                return SESSIONS[token]
            # Tenta recarregar do disco caso as sessões em memória tenham sido limpas
            fresh_sessions = load_sessions()
            if token in fresh_sessions:
                SESSIONS[token] = fresh_sessions[token]
                return fresh_sessions[token]
            
        return None

    def send_cors_response(self, status=200, content_type='application/json'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.end_headers()

    def do_OPTIONS(self):
        self.send_cors_response(200)

    def do_GET(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        
        # Servir Service Worker (público)
        if path in ('/sw.js', '/dashboard/sw.js'):
            self.send_cors_response(200, 'application/javascript; charset=utf-8')
            sw_path = 'dashboard/sw.js' if os.path.exists('dashboard/sw.js') else 'sw.js'
            if os.path.exists(sw_path):
                with open(sw_path, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self.wfile.write(b'// SW v2 Clean Cache\nself.addEventListener("install", e => self.skipWaiting());\nself.addEventListener("activate", e => e.waitUntil(caches.keys().then(keys => Promise.all(keys.map(k => caches.delete(k)))).then(() => self.clients.claim())));')
            return

        # Servir a página de Login (pública)
        if path == '/login.html':
            self.send_cors_response(200, 'text/html; charset=utf-8')
            with open('dashboard/login.html', 'rb') as f:
                self.wfile.write(f.read())
            return

        # Para APIs e arquivos estáticos restritos, verifica autenticação
        user = self.get_authenticated_user()

        # Servir a página principal ou login (proteção de rota unificada sem redirecionamento HTTP 302)
        if path in ('/', '/index.html'):
            self.send_cors_response(200, 'text/html; charset=utf-8')
            if not user:
                with open('dashboard/login.html', 'rb') as f:
                    self.wfile.write(f.read())
            else:
                with open('dashboard/index.html', 'rb') as f:
                    self.wfile.write(f.read())
            return



        # Servir os arquivos temporários locais (Imagens de thumbnail)
        if path.startswith('/temp_uploads/'):

            if not user:
                self.send_cors_response(401)
                self.wfile.write(json.dumps({"error": "Unauthorized"}).encode('utf-8'))
                return

            filename = os.path.basename(path)
            file_path = os.path.join('temp_uploads', filename)
            
            if os.path.exists(file_path):
                mime = 'image/png' if filename.endswith('.png') else 'image/jpeg'
                self.send_cors_response(200, mime)
                with open(file_path, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self.send_cors_response(404, 'text/plain')
                self.wfile.write(b'File Not Found')
                
        # API: Obter todos os produtos cadastrados no Google Sheets (suporta /api/products e /api/data)
        elif path in ('/api/products', '/api/data'):
            if not user:
                self.send_cors_response(401)
                self.wfile.write(json.dumps({"error": "Unauthorized"}).encode('utf-8'))
                return

            products = []
            try:
                creds = authenticate()
                if creds:
                    sheet_id = setup_google_sheet(PARENT_FOLDER_ID, creds)
                    if sheet_id and sheet_id != "mock_sheet_id":
                        from googleapiclient.discovery import build
                        sheets_service = build("sheets", "v4", credentials=creds)
                        sheet_name = get_first_sheet_name(sheets_service, sheet_id)
                        result = safe_sheets_get(
                            sheets_service=sheets_service,
                            spreadsheet_id=sheet_id,
                            sheet_name=sheet_name,
                            cell_range="A2:K200"
                        )
                        
                        rows = result.get('values', [])
                        for idx, row in enumerate(rows):
                            while len(row) < len(HEADERS):
                                row.append("")
                                
                            titulo = row[1]
                            local_thumb = "/temp_uploads/test_product.jpg"
                            if "fone" in titulo.lower():
                                local_thumb = "/temp_uploads/fone_bluetooth.jpg"
                            elif "revisar" in titulo.lower() or "defeito" in titulo.lower() or "nome!" in titulo.lower():
                                local_thumb = "/temp_uploads/test_revisar.jpg"
                            
                            products.append({
                                "row_num": idx + 2,
                                "id": row[0],
                                "titulo": row[1],
                                "categoria": row[2],
                                "preco": row[3],
                                "estoque": row[4],
                                "condicao": row[5],
                                "url_fotos": row[6],
                                "status": row[7],
                                "review_needed": row[8] == "SIM",
                                "motivo_revisao": row[9],
                                "date": row[10],
                                "local_thumb": local_thumb,
                                "original_filename": "fone_bluetooth.jpg" if "fone" in titulo.lower() else "test_revisar.jpg"
                            })
                else:
                    print("ℹ️ [GET /api/products] Credenciais do Google nulas. Utilizando lista local de fallback.")
            except Exception as e:
                import traceback
                print(f"⚠️ [GET /api/products] Erro ao consultar Google Sheets ({e}). Utilizando fallback local.")
                traceback.print_exc()

            # Resiliência: se Sheets não retornar produtos ou falhar, mescla/utiliza o armazenamento local
            if not products:
                products = list(LOCAL_PRODUCTS)
            else:
                for lp in LOCAL_PRODUCTS:
                    if not any(sp.get("id") == lp.get("id") for sp in products):
                        products.append(lp)

            # Sempre responde com 200 OK e JSON array válido de forma segura contra desconexões de socket
            self.safe_write_response(200, products)


        else:
            self.send_cors_response(404, 'text/plain')
            self.wfile.write(b'Not Found')

    def do_POST(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        
        # API: Login de Usuários (pública)
        if path == '/api/login':
            try:
                content_length = int(self.headers.get('content-length', 0))
                body = self.rfile.read(content_length)
                data = json.loads(body.decode('utf-8'))
                
                username = data.get("username")
                password = data.get("password")
                
                user_role = None
                if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
                    user_role = "owner"
                elif username == USER2_USERNAME and password == USER2_PASSWORD:
                    user_role = "collaborator"
                    
                if user_role:
                    session_token = str(uuid.uuid4())
                    SESSIONS[session_token] = {
                        "username": username,
                        "role": user_role,
                        "created_at": format_brasilia_time("%d/%m/%Y %H:%M:%S")

                    }
                    save_sessions(SESSIONS)
                    
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    # Cookie válido por 30 dias (2592000 segundos) para manter o login durante testes no Render
                    self.send_header('Set-Cookie', f'session_token={session_token}; Path=/; Max-Age=2592000; SameSite=Lax')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "success": True,
                        "token": session_token,
                        "user": {
                            "username": username,
                            "role": user_role
                        }
                    }).encode('utf-8'))
                    return
                else:
                    self.send_cors_response(401)
                    self.wfile.write(json.dumps({"error": "Credenciais inválidas."}).encode('utf-8'))
                    return
            except Exception as e:
                self.send_cors_response(500)
                self.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))
                return

        # API: Logout de Usuários (pública)
        elif path == '/api/logout':
            cookie_header = self.headers.get('Cookie')
            token = None
            if cookie_header:
                cookies = re.findall(r'session_token=([^;]+)', cookie_header)
                if cookies:
                    token = cookies[0].strip()
            if not token:
                auth_header = self.headers.get('Authorization')
                if auth_header and auth_header.startswith('Bearer '):
                    token = auth_header.split('Bearer ', 1)[1].strip()
                    
            if token and token in SESSIONS:
                del SESSIONS[token]
                save_sessions(SESSIONS)
                
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Set-Cookie', 'session_token=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT; SameSite=Lax')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()

            self.wfile.write(json.dumps({"success": True}).encode('utf-8'))
            return

        # Para as demais rotas POST, exige autenticação
        user = self.get_authenticated_user()
        if not user:
            self.send_cors_response(401)
            self.wfile.write(json.dumps({"error": "Unauthorized"}).encode('utf-8'))
            return

        # API: Upload de imagens via JSON Base64 (Leitura ultraleve sem multipart)
        if path == '/api/upload':
            try:
                content_length = int(self.headers.get('content-length', 0))
                if content_length <= 0:
                    self.safe_write_response(400, {"error": "Corpo da requisição vazio."})
                    return
                
                body_bytes = self.rfile.read(content_length)
                payload = json.loads(body_bytes.decode('utf-8'))
                del body_bytes
                gc.collect()

                raw_images = payload.get("images", [])
                if isinstance(raw_images, str):
                    raw_images = [raw_images]
                    
                if not raw_images or not isinstance(raw_images, list):
                    self.safe_write_response(400, {"error": "Nenhuma imagem em formato JSON Base64 enviada."})
                    return

                # Limita a no máximo 3 fotos por envio
                if len(raw_images) > 3:
                    raw_images = raw_images[:3]

                os.makedirs("temp_uploads", exist_ok=True)
                saved_paths = []
                from scripts.vision_processor import clean_and_decode_image_bytes, analyze_product_image

                # Decodifica e salva cada imagem base64 uma por uma em arquivo temporário no disco
                for idx, b64_item in enumerate(raw_images):
                    if not b64_item:
                        continue
                    
                    filename = f"upload_{format_brasilia_time('%Y%m%d_%H%M%S')}_{idx+1}.jpg"
                    file_path = os.path.join("temp_uploads", filename)
                    
                    clean_bytes = clean_and_decode_image_bytes(b64_item)
                    with open(file_path, 'wb') as f:
                        f.write(clean_bytes)
                    saved_paths.append(file_path)
                    
                    del clean_bytes
                    raw_images[idx] = None
                    gc.collect()

                del raw_images, payload
                gc.collect()

                if not saved_paths:
                    self.safe_write_response(400, {"error": "Falha ao decodificar imagens enviadas."})
                    return


                
                # 1. Executa a análise local da imagem principal de forma síncrona com import sob demanda
                main_image = saved_paths[0]
                product_data = analyze_product_image(main_image)
                requires_review = product_data.get("requires_manual_review", False)
                gc.collect()

                # Persistência imediata e garantida no cache local de produtos (salva no disco ANTES de responder ao socket)
                product_id = f"MLB{uuid.uuid4().hex[:10].upper()}"
                initial_local_item = {
                    "row_num": len(LOCAL_PRODUCTS) + 2,
                    "id": product_id,
                    "titulo": product_data.get("titulo", "Produto Enviado"),
                    "categoria": product_data.get("categoria", "Outros"),
                    "preco": product_data.get("preco_sugerido", 50.0),
                    "estoque": product_data.get("estoque", 1),
                    "condicao": product_data.get("condicao", "used"),
                    "url_fotos": "/temp_uploads/" + os.path.basename(main_image) if os.path.exists(main_image) else "/temp_uploads/test_product.jpg",
                    "status": "REVISAO_MANUAL" if requires_review else "PENDENTE",
                    "review_needed": requires_review,
                    "motivo_revisao": product_data.get("review_reason", ""),
                    "date": format_brasilia_time("%d/%m/%Y %H:%M:%S"),
                    "local_thumb": "/temp_uploads/" + os.path.basename(main_image) if os.path.exists(main_image) else "/temp_uploads/test_product.jpg",
                    "original_filename": os.path.basename(main_image)
                }
                LOCAL_PRODUCTS[:] = [p for p in LOCAL_PRODUCTS if p.get("id") != product_id]
                LOCAL_PRODUCTS.append(initial_local_item)
                save_local_products(LOCAL_PRODUCTS)
                gc.collect()

                # 2. Executa o restante do pipeline (otimização, upload ao Drive, Sheets e ML) em worker background 100% isolado
                import threading
                def async_pipeline_worker():
                    try:
                        run_pipeline(saved_paths, dry_run=True, product_id=product_id)
                    except Exception as err:
                        import traceback
                        print(f"❌ Erro no pipeline assíncrono: {err}")
                        traceback.print_exc()
                    finally:
                        cleanup_temp_files(saved_paths)
                        gc.collect()
                
                threading.Thread(target=async_pipeline_worker, daemon=True).start()

                # 3. Retorna resposta HTTP síncrona de forma segura contra BrokenPipeError / desconexão de cliente
                result = {
                    "success": True,
                    "requires_manual_review": requires_review,
                    "product_data": product_data
                }
                self.safe_write_response(200, result)

                
            except Exception as e:
                import traceback
                print(f"❌ [UPLOAD HANDLER ERROR] Falha no endpoint /api/upload: {e}")
                traceback.print_exc()
                gc.collect()
                
                # Resiliência: em caso de exceção de parse, retorna resposta 200 de fallback marcada para revisão manual
                fallback_product = {
                    "titulo": "Produto Enviado (Revisar Cadastro)",
                    "categoria": "Outros",
                    "preco_sugerido": 50.00,
                    "estoque": 1,
                    "condicao": "used",
                    "descricao": f"Erro de upload/processamento: {str(e)}. Por favor, preencha manualmente.",
                    "confidence_score": "0.0%",
                    "requires_manual_review": True,
                    "review_reason": f"Falha ao processar upload ({str(e)})."
                }
                self.send_cors_response(200)
                self.wfile.write(json.dumps({
                    "success": True,
                    "requires_manual_review": True,
                    "product_data": fallback_product
                }).encode('utf-8'))
            finally:
                gc.collect()


        # API: Salvar modificações da Revisão Manual e Aprovar Publicação (sem reprocessamento de imagem)
        elif path in ('/api/review', '/api/products/update'):
            try:
                content_length = int(self.headers.get('content-length', 0))
                body = self.rfile.read(content_length)
                data = json.loads(body.decode('utf-8'))
                del body
                
                raw_idx = data.get("index")
                try:
                    idx = int(raw_idx) if raw_idx is not None else None
                except Exception:
                    idx = None
                    
                raw_row = data.get("row_num")
                if raw_row is not None:
                    try:
                        row_num = int(raw_row)
                    except Exception:
                        row_num = (idx + 2) if idx is not None else 2
                else:
                    row_num = (idx + 2) if idx is not None else 2

                product_id = str(data.get("id") or data.get("product_id") or "").strip()
                
                titulo = sanitize_title(data.get("titulo", ""))
                categoria = data.get("categoria", "Outros")
                preco = float(data.get("preco", data.get("preco_sugerido", 0.0)))
                estoque = int(data.get("estoque", 1))
                descricao = data.get("descricao", "")
                condicao = data.get("condicao", "new")
                
                status = "HOMOLOGADO"
                motivo = "Aprovado manualmente via interface de controle."
                
                print(f"\n✍️ [REVISÃO MANUAL] Aprovação rápida para produto (linha {row_num}): '{titulo}'")
                
                product_data = {
                    "titulo": titulo,
                    "categoria": categoria,
                    "preco_sugerido": preco,
                    "estoque": estoque,
                    "condicao": condicao,
                    "descricao": descricao
                }
                
                # 1. Atualização imediata no cache local de produtos (sem reprocessar imagem)
                updated = False
                for p in LOCAL_PRODUCTS:
                    if (product_id and str(p.get("id")) == product_id) or \
                       str(p.get("row_num")) == str(row_num) or \
                       (idx is not None and 0 <= idx < len(LOCAL_PRODUCTS) and LOCAL_PRODUCTS[idx] == p) or \
                       (titulo and p.get("titulo") == titulo):

                        p["titulo"] = titulo
                        p["categoria"] = categoria
                        p["preco"] = preco
                        p["estoque"] = estoque
                        p["descricao"] = descricao
                        p["status"] = status
                        p["review_needed"] = False
                        p["motivo_revisao"] = motivo
                        updated = True
                        break
                
                if not updated:
                    LOCAL_PRODUCTS.append({
                        "row_num": row_num,
                        "id": product_id or f"MLB{uuid.uuid4().hex[:10].upper()}",
                        "titulo": titulo,
                        "categoria": categoria,
                        "preco": preco,
                        "estoque": estoque,
                        "descricao": descricao,
                        "condicao": condicao,
                        "url_fotos": data.get("url_fotos", "/temp_uploads/test_product.jpg"),
                        "status": status,
                        "review_needed": False,
                        "motivo_revisao": motivo,
                        "date": format_brasilia_time("%d/%m/%Y %H:%M:%S"),

                        "local_thumb": "/temp_uploads/test_product.jpg"
                    })

                save_local_products(LOCAL_PRODUCTS)
                gc.collect()

                # 2. Envia resposta HTTP 200 OK JSON IMEDIATA ao frontend (evita BrokenPipeError)
                self.send_cors_response(200)
                self.wfile.write(json.dumps({
                    "success": True,
                    "status": status,
                    "message": "Produto aprovado e homologado com sucesso."
                }).encode('utf-8'))

                # 3. Executa a atualização na planilha remota do Google Sheets em thread de background assíncrona
                import threading
                def async_sheets_update():
                    try:
                        creds = authenticate()
                        sheet_id = setup_google_sheet(PARENT_FOLDER_ID, creds)
                        update_product_in_sheet(
                            sheet_id=sheet_id,
                            row_num=row_num,
                            product_data=product_data,
                            status=status,
                            review_needed=False,
                            review_reason=motivo,
                            creds=creds,
                            product_id=product_id
                        )
                    except Exception as sheet_err:
                        print(f"⚠️ Aviso: Erro ao atualizar planilha remota em background: {sheet_err}")
                    finally:
                        gc.collect()

                threading.Thread(target=async_sheets_update, daemon=True).start()

            except Exception as e:
                import traceback
                print(f"❌ Erro ao processar aprovação manual: {e}")
                traceback.print_exc()
                gc.collect()
                self.send_cors_response(500)
                self.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))
            finally:
                gc.collect()


        else:
            self.send_cors_response(404, 'text/plain')
            self.wfile.write(b'Not Found')

    def do_DELETE(self):
        # Exige autenticação para DELETE
        user = self.get_authenticated_user()
        if not user:
            self.send_cors_response(401)
            self.wfile.write(json.dumps({"error": "Unauthorized"}).encode('utf-8'))
            return

        parsed_url = urlparse(self.path)
        path = parsed_url.path
        
        # Suporta DELETE /api/products/<product_id>?row_num=<row_num>
        match = re.match(r'^/api/products/([^/]+)$', path)
        if match:
            product_id = match.group(1)
            query = parse_qs(parsed_url.query)
            row_num_str = query.get('row_num', [None])[0]
            
            if not row_num_str:
                self.send_cors_response(400)
                self.wfile.write(json.dumps({"error": "Parâmetro row_num é obrigatório."}).encode('utf-8'))
                return
                
            try:
                row_num = int(row_num_str)
                print(f"\n🗑️ Recebida requisição de exclusão em cascata:")
                print(f"   ID Produto: {product_id}")
                print(f"   Linha Planilha: {row_num}")
                
                creds = authenticate(allow_interactive=False)
                if creds:
                    sheet_id = setup_google_sheet(PARENT_FOLDER_ID, creds)
                    if sheet_id and sheet_id != "mock_sheet_id":
                        from googleapiclient.discovery import build
                        sheets_service = build("sheets", "v4", credentials=creds)
                        
                        sheet_name = get_first_sheet_name(sheets_service, sheet_id)
                        # Obtém a linha correspondente da planilha
                        result = safe_sheets_get(
                            sheets_service=sheets_service,
                            spreadsheet_id=sheet_id,
                            sheet_name=sheet_name,
                            cell_range=f"A{row_num}:K{row_num}"
                        )
                        
                        rows = result.get('values', [])
                        if rows:
                            row_data = rows[0]
                            url_fotos = row_data[6] if len(row_data) > 6 else ""
                            
                            # 2. Excluir os arquivos no Google Drive
                            if url_fotos:
                                urls = [u.strip() for u in url_fotos.split(',') if u.strip()]
                                for url in urls:
                                    delete_drive_file(url, creds)
                        
                        # 4. Excluir a linha na planilha do Google Sheets
                        delete_sheet_row(sheet_id, row_num, creds)
                
                # 3. Excluir o anúncio no Mercado Livre (se houver MLB válido)
                publisher = MLPublisher(access_token="mock_token_abc123")
                publisher.delete_item(product_id, dry_run=True)
                
                # 5. Excluir do armazenamento local de produtos
                LOCAL_PRODUCTS[:] = [p for p in LOCAL_PRODUCTS if str(p.get("id")) != str(product_id) and str(p.get("row_num")) != str(row_num_str)]
                save_local_products(LOCAL_PRODUCTS)
                
                self.send_cors_response(200)
                self.wfile.write(json.dumps({"success": True, "message": "Exclusão em cascata concluída."}).encode('utf-8'))
                
            except Exception as e:
                import traceback
                print(f"❌ Erro ao executar exclusão em cascata: {e}")
                traceback.print_exc()
                # Exclui do cache local mesmo se Google API falhar
                LOCAL_PRODUCTS[:] = [p for p in LOCAL_PRODUCTS if str(p.get("id")) != str(product_id) and str(p.get("row_num")) != str(row_num_str)]
                save_local_products(LOCAL_PRODUCTS)
                self.send_cors_response(200)
                self.wfile.write(json.dumps({"success": True, "message": "Exclusão concluída no armazenamento local."}).encode('utf-8'))

        else:
            self.send_cors_response(404, 'text/plain')
            self.wfile.write(b'Not Found')

def get_local_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

class OptimizedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 5

def run_server(port=8000):
    server_address = ('0.0.0.0', port)
    httpd = OptimizedThreadingHTTPServer(server_address, DashboardHTTPHandler)
    local_ip = get_local_ip()
    print(f"\n🌐 Servidor Web otimizado para Render (512MB RAM) iniciado na porta {port}!")
    print(f"👉 Acesso Local: http://localhost:{port}")
    print(f"📱 Acesso na Rede (Celular/Wi-Fi): http://{local_ip}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Servidor encerrado.")
        httpd.server_close()

if __name__ == "__main__":
    os.makedirs("temp_uploads", exist_ok=True)
    
    test_ok_path = "temp_uploads/fone_bluetooth.jpg"
    test_fail_path = "temp_uploads/test_revisar.jpg"
    
    if not os.path.exists(test_ok_path) or not os.path.exists(test_fail_path):
        from PIL import Image
        import numpy as np
        if not os.path.exists(test_ok_path):
            arr = np.random.randint(0, 255, (800, 800, 3), dtype=np.uint8)
            img = Image.fromarray(arr)
            img.save(test_ok_path)
            print(f"📷 Imagem de teste válida criada em: {test_ok_path}")
            
        if not os.path.exists(test_fail_path):
            img = Image.new('RGB', (100, 100), color='grey')
            img.save(test_fail_path)
            print(f"📷 Imagem de teste inválida criada em: {test_fail_path}")
        gc.collect()
        
    port = int(os.environ.get("PORT", 10000))
    # Verifica argumentos de terminal
    if len(sys.argv) > 1 and sys.argv[1] == "--server-only":
        run_server(port)
    else:
        print("Executando rodada de testes do pipeline...")
        # Executa testes com lista de imagens
        run_pipeline([test_ok_path], dry_run=True)
        run_pipeline([test_fail_path], dry_run=True)
        
        run_server(port)

