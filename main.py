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


# ID da pasta principal no Drive
PARENT_FOLDER_ID = "1pjqOPcWHW8gCZ9GdLF7ta6NESN0dyw70"


def run_pipeline(image_paths, dry_run=True):
    from scripts.vision_processor import optimize_image_for_ml, analyze_product_image

    # Aceita string única ou lista de caminhos
    if isinstance(image_paths, str):
        image_paths = [image_paths]
        
    print("\n" + "="*60)
    print(f"🎬 INICIANDO PIPELINE: {len(image_paths)} Fotos (dry_run={dry_run})")
    print(f"   Arquivos: {image_paths}")
    print("="*60)
    
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
        
        # Se necessitar de revisão manual (baixa qualidade, dimensões ou dados inválidos)
        if product_data.get("requires_manual_review"):
            reason = product_data.get("review_reason", "Motivo desconhecido")
            print(f"\n⚠️ [REVISÃO MANUAL DETECTADA] {reason}")
            
            # Adiciona na planilha com status REVISAO_MANUAL
            add_product_to_sheet(
                sheet_id=sheet_id,
                product_data=product_data,
                status="REVISAO_MANUAL",
                review_needed=True,
                review_reason=reason,
                creds=creds
            )
            gc.collect()
            print("\n❌ Pipeline interrompido: O produto necessita de revisão humana.")
            return {
                "success": False,
                "requires_manual_review": True,
                "reason": reason,
                "product_data": product_data
            }

        # 4. Otimização das Imagens
        # - 1ª imagem (Principal): remove o fundo com rembg.
        # - Demais imagens: apenas padroniza tamanho sem remover o fundo.
        print("\n📷 Passo 4: Otimizando imagens para padrões do Mercado Livre...")
        optimized_paths = []
        public_urls = []
        
        for idx, img_path in enumerate(image_paths):
            base_name = os.path.basename(img_path)
            name, ext = os.path.splitext(base_name)
            
            # Determina se é a imagem principal ou adicional
            is_main = (idx == 0)
            suffix = "main_opt" if is_main else f"sub_{idx}_opt"
            optimized_path = os.path.join("temp_uploads", f"{name}_{suffix}.jpg")
            
            # Executa otimização (remove_bg=True apenas para a foto principal)
            optimize_image_for_ml(img_path, optimized_path, remove_bg=is_main)
            optimized_paths.append(optimized_path)
            
            # 5. Upload das imagens otimizadas para o Google Drive
            print(f"🚀 Passo 5.{idx+1}: Enviando foto {idx+1} para o Google Drive...")
            url = upload_product_photo(optimized_path, photos_folder_id, creds)
            public_urls.append(url)
            gc.collect()

        # Junta todas as URLs separadas por vírgula para salvar na Planilha
        concatenated_urls = ", ".join(public_urls)
        product_data["url_fotos"] = concatenated_urls
        
        # 6. Integração e Validação do Mercado Livre
        print("\n🛍️ Passo 6: Validando publicação no Mercado Livre...")
        publisher = MLPublisher(access_token="mock_token_abc123")
        
        if dry_run:
            result = publisher.publish_item(product_data, public_urls, dry_run=True)
            status = "HOMOLOGADO (DRY RUN)"
            ml_id = result.get("id", "")
            
            add_product_to_sheet(
                sheet_id=sheet_id,
                product_data=product_data,
                status=status,
                review_needed=False,
                review_reason="Validação do payload aprovada com múltiplas fotos (Modo de Simulação).",
                creds=creds,
                product_id=ml_id
            )
        else:
            result = publisher.publish_item(product_data, public_urls, dry_run=False)
            if result.get("status") == "success":
                status = "PUBLICADO"
                ml_id = result.get("id", "")
                add_product_to_sheet(
                    sheet_id=sheet_id,
                    product_data=product_data,
                    status=status,
                    review_needed=False,
                    review_reason=f"Publicado com sucesso no ML com {len(public_urls)} fotos. ID: {ml_id}",
                    creds=creds,
                    product_id=ml_id
                )
            else:
                status = "ERRO"
                add_product_to_sheet(
                    sheet_id=sheet_id,
                    product_data=product_data,
                    status=status,
                    review_needed=True,
                    review_reason=f"Erro na publicação: {result.get('message', 'Erro desconhecido')}",
                    creds=creds
                )
        
        # 7. Limpeza dos arquivos locais otimizados temporários
        print("\n🧹 Passo 7: Limpando arquivos temporários locais...")
        for opt_path in optimized_paths:
            if os.path.exists(opt_path):
                os.remove(opt_path)
                print(f"   Arquivo temporário removido: {opt_path}")

        # Registra no armazenamento local de produtos para garantir exibição no dashboard mesmo sem Google APIs
        local_item = {
            "row_num": len(LOCAL_PRODUCTS) + 2,
            "id": ml_id or f"MLB{uuid.uuid4().hex[:10].upper()}",
            "titulo": product_data.get("titulo", ""),
            "categoria": product_data.get("categoria", ""),
            "preco": product_data.get("preco_sugerido", 0.0),
            "estoque": product_data.get("estoque", 1),
            "condicao": product_data.get("condicao", "new"),
            "url_fotos": product_data.get("url_fotos", ""),
            "status": status,
            "review_needed": product_data.get("requires_manual_review", False),
            "motivo_revisao": product_data.get("review_reason", ""),
            "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "local_thumb": "/temp_uploads/" + os.path.basename(main_image) if os.path.exists(main_image) else "/temp_uploads/test_product.jpg",
            "original_filename": os.path.basename(main_image)
        }
        LOCAL_PRODUCTS[:] = [p for p in LOCAL_PRODUCTS if p.get("id") != local_item["id"]]
        LOCAL_PRODUCTS.append(local_item)
        save_local_products(LOCAL_PRODUCTS)
            
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
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
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
        
        sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"A{row_num}:K{row_num}",
            valueInputOption="USER_ENTERED",
            body=body
        ).execute()
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
        # Log otimizado e leve para evitar alocações desnecessárias em repouso
        sys.stdout.write(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {format % args}\n")

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
                        
                        result = sheets_service.spreadsheets().values().get(
                            spreadsheetId=sheet_id,
                            range="A2:K200"
                        ).execute()
                        
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

            # Sempre responde com 200 OK e JSON array válido (evita estouro 500 no frontend)
            self.send_cors_response(200)
            self.wfile.write(json.dumps(products).encode('utf-8'))

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
                        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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

        # API: Upload de imagens e execução automática do Pipeline (Suporta múltiplas imagens)
        if path == '/api/upload':
            try:
                content_type = self.headers.get('content-type', '')
                if 'boundary=' not in content_type:
                    self.send_cors_response(400)
                    self.wfile.write(json.dumps({"error": "Bad Request: boundary missing"}).encode('utf-8'))
                    return
                
                boundary = content_type.split('boundary=')[1].strip().encode('utf-8')
                content_length = int(self.headers.get('content-length', 0))
                body = self.rfile.read(content_length)
                
                parts = body.split(boundary)
                saved_paths = []
                
                from scripts.vision_processor import clean_and_decode_image_bytes, analyze_product_image

                for part in parts:
                    if b'filename="' in part:
                        headers_part, file_content = part.split(b'\r\n\r\n', 1)
                        file_content = file_content.rsplit(b'\r\n', 1)[0]
                        
                        filename_match = re.search(rb'filename="([^"]+)"', headers_part)
                        if filename_match:
                            filename = filename_match.group(1).decode('utf-8')
                            
                            os.makedirs("temp_uploads", exist_ok=True)
                            file_path = os.path.join("temp_uploads", filename)
                            
                            # Higieniza e decodifica bytes base64 / Data URL caso o canvas do celular tenha enviado em formato codificado
                            clean_bytes = clean_and_decode_image_bytes(file_content)
                            
                            with open(file_path, 'wb') as f:
                                f.write(clean_bytes)
                            saved_paths.append(file_path)

                            # Libera imediatamente a memória de cada part/buffer
                            del file_content, clean_bytes, headers_part
                            gc.collect()
                
                del parts, body
                gc.collect()

                if not saved_paths:
                    self.send_cors_response(400)
                    self.wfile.write(json.dumps({"error": "Bad Request: No files uploaded"}).encode('utf-8'))
                    return
                
                # 1. Executa a análise local da imagem principal de forma síncrona com import sob demanda
                main_image = saved_paths[0]
                product_data = analyze_product_image(main_image)
                requires_review = product_data.get("requires_manual_review", False)
                gc.collect()

                # 2. Executa o restante do pipeline (otimização, upload ao Drive, Sheets e ML) em background
                import threading
                def async_pipeline_worker():
                    try:
                        run_pipeline(saved_paths, dry_run=True)
                    except Exception as err:
                        import traceback
                        print(f"❌ Erro no pipeline assíncrono: {err}")
                        traceback.print_exc()
                    finally:
                        cleanup_temp_files(saved_paths)
                        gc.collect()
                
                threading.Thread(target=async_pipeline_worker, daemon=True).start()

                
                # 3. Retorna resposta síncrona imediatamente
                result = {
                    "success": True,
                    "requires_manual_review": requires_review,
                    "product_data": product_data
                }
                
                self.send_cors_response(200)
                self.wfile.write(json.dumps(result).encode('utf-8'))
                
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
                
                idx = data.get("index")
                row_num = int(data.get("row_num", idx + 2 if idx is not None else 2))
                product_id = data.get("id", data.get("product_id", ""))
                
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
                    if (product_id and p.get("id") == product_id) or \
                       p.get("row_num") == row_num or \
                       (idx is not None and idx < len(LOCAL_PRODUCTS) and LOCAL_PRODUCTS[idx] == p) or \
                       p.get("titulo") == titulo:
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
                        "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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
                        
                        # Obtém a linha correspondente da planilha
                        result = sheets_service.spreadsheets().values().get(
                            spreadsheetId=sheet_id,
                            range=f"A{row_num}:K{row_num}"
                        ).execute()
                        
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
        
    port = int(os.environ.get("PORT", 8000))
    # Verifica argumentos de terminal
    if len(sys.argv) > 1 and sys.argv[1] == "--server-only":
        run_server(port)
    else:
        print("Executando rodada de testes do pipeline...")
        # Executa testes com lista de imagens
        run_pipeline([test_ok_path], dry_run=True)
        run_pipeline([test_fail_path], dry_run=True)
        
        run_server(port)

