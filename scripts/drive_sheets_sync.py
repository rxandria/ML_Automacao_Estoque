# -*- coding: utf-8 -*-
"""
Google Drive & Sheets synchronization script.
"""
import os.path
import mimetypes
import datetime
import uuid
import gc
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

from google.auth.transport.requests import Request

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

# Escopos de permissão solicitados
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets"
]

CREDENTIALS_FILE = "config/credentials.json"
TOKEN_FILE = "config/token.json"

HEADERS = [
    "ID Produto", "Título", "Categoria", "Preço (R$)", 
    "Estoque", "Condição", "URL Fotos", "Status ML", 
    "Revisão Necessária", "Motivo Revisão", "Data Criação"
]

from google.oauth2 import service_account

def authenticate(allow_interactive=False):
    creds = None
    import traceback
    import json
    
    # 1. Tenta Service Account a partir de GOOGLE_CREDENTIALS_JSON (String JSON de Service Account em ambiente Cloud Run)
    google_credentials_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if google_credentials_json:
        try:
            creds_data = json.loads(google_credentials_json)
            if isinstance(creds_data, dict) and creds_data.get("type") == "service_account":
                creds = service_account.Credentials.from_service_account_info(creds_data, scopes=SCOPES)
                print("🔑 [GOOGLE AUTH SUCCESS] Service Account carregada com sucesso via GOOGLE_CREDENTIALS_JSON.")
                return creds
            elif isinstance(creds_data, dict) and creds_data.get("type") == "authorized_user":
                creds = Credentials.from_authorized_user_info(creds_data, SCOPES)
                print("🔑 [GOOGLE AUTH SUCCESS] Authorized User carregado via GOOGLE_CREDENTIALS_JSON.")
                return creds
            elif isinstance(creds_data, dict) and ("installed" in creds_data or "web" in creds_data):
                # OAuth Client Secret JSON fornecido via env var
                if allow_interactive:
                    flow = InstalledAppFlow.from_client_config(creds_data, SCOPES)
                    creds = flow.run_local_server(port=0)
                    return creds
                else:
                    print("⚠️ [GOOGLE AUTH WARNING] Client Config OAuth recebido em GOOGLE_CREDENTIALS_JSON, mas ambiente não é interativo (necessária Service Account).")
        except Exception as e:
            print(f"❌ [GOOGLE AUTH ERROR] Erro ao decodificar GOOGLE_CREDENTIALS_JSON: {e}")
            traceback.print_exc()
            creds = None

    # 2. Tenta Service Account a partir de GOOGLE_APPLICATION_CREDENTIALS (Caminho para arquivo JSON)
    app_credentials_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if app_credentials_path and os.path.exists(app_credentials_path):
        try:
            creds = service_account.Credentials.from_service_account_file(app_credentials_path, scopes=SCOPES)
            print(f"🔑 [GOOGLE AUTH SUCCESS] Service Account carregada do arquivo: {app_credentials_path}")
            return creds
        except Exception as e:
            print(f"❌ [GOOGLE AUTH ERROR] Erro ao carregar Service Account do arquivo {app_credentials_path}: {e}")
            traceback.print_exc()
            creds = None

    # 3. Tenta carregar credenciais de usuário OAuth via GOOGLE_TOKEN_JSON
    google_token_json = os.environ.get("GOOGLE_TOKEN_JSON")
    if google_token_json:
        try:
            token_data = json.loads(google_token_json)
            creds = Credentials.from_authorized_user_info(token_data, SCOPES)
            print("🔑 [GOOGLE AUTH SUCCESS] Credenciais OAuth carregadas de GOOGLE_TOKEN_JSON.")
        except Exception as e:
            print(f"⚠️ [GOOGLE AUTH ERROR] Erro ao carregar GOOGLE_TOKEN_JSON: {e}")
            traceback.print_exc()
            creds = None

    # 4. Fallback para carregar do arquivo local token.json
    if not creds and os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            print("🔑 Credenciais do Google carregadas do token.json local.")
        except Exception as e:
            print(f"⚠️ [GOOGLE AUTH ERROR] Erro ao carregar token.json local: {e}")
            traceback.print_exc()
            creds = None

    # 5. Validação / Renovação de token
    if creds and not creds.valid:
        if creds.expired and getattr(creds, "refresh_token", None):
            try:
                creds.refresh(Request())
                print("🔄 Token de acesso do Google renovado com sucesso.")
            except Exception as e:
                print(f"⚠️ [GOOGLE AUTH ERROR] Erro ao renovar token do Google: {e}")
                traceback.print_exc()
                creds = None

    if not creds or not creds.valid:
        if not allow_interactive:
            print("❌ [GOOGLE AUTH ERROR] Credenciais do Google não configuradas ou inválidas (Defina GOOGLE_CREDENTIALS_JSON ou GOOGLE_APPLICATION_CREDENTIALS).")
            return None

        # Interativo local
        if os.path.exists(CREDENTIALS_FILE):
            try:
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
                creds = flow.run_local_server(port=0)
                try:
                    with open(TOKEN_FILE, "w") as token:
                        token.write(creds.to_json())
                except Exception as e:
                    print(f"⚠️ Erro ao salvar token.json localmente: {e}")
            except Exception as e:
                print(f"❌ Erro no fluxo interativo local: {e}")
                creds = None

    return creds

def setup_drive_structure(folder_id, creds):
    """
    Verifica se a pasta 'Fotos_Produtos' existe dentro de folder_id.
    Lê a variável de ambiente DRIVE_FOLDER_ID como prioridade.
    """
    env_folder_id = os.environ.get("DRIVE_FOLDER_ID")
    folder_id = env_folder_id or folder_id or "1pjqOPcWHW8gCZ9GdLF7ta6NESN0dyw70"

    if not creds:
        print("❌ [GOOGLE DRIVE ERROR] Credenciais ausentes. Impossível configurar pasta no Google Drive.")
        return None
    try:
        service = build("drive", "v3", credentials=creds)
        
        query = (
            f"name = 'Fotos_Produtos' and '{folder_id}' in parents "
            f"and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        results = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
        files = results.get('files', [])
        
        if files:
            photos_folder_id = files[0]['id']
            print(f"📁 Pasta 'Fotos_Produtos' já existe. ID: {photos_folder_id}")
        else:
            print("📁 Criando pasta 'Fotos_Produtos'...")
            file_metadata = {
                'name': 'Fotos_Produtos',
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [folder_id]
            }
            file = service.files().create(body=file_metadata, fields='id').execute()
            photos_folder_id = file.get('id')
            print(f"📁 Pasta 'Fotos_Produtos' criada com sucesso! ID: {photos_folder_id}")
            
        return photos_folder_id
        
    except Exception as error:
        import traceback
        print(f"❌ [GOOGLE DRIVE ERROR] Erro ao configurar estrutura do Drive: {error}")
        traceback.print_exc()
        return None

def setup_google_sheet(folder_id, creds):
    """
    Verifica se a planilha existe ou retorna a planilha configurada via SPREADSHEET_ID.
    """
    env_sheet_id = os.environ.get("SPREADSHEET_ID")
    if env_sheet_id:
        print(f"📄 [GOOGLE SHEETS] Usando SPREADSHEET_ID das variáveis de ambiente: {env_sheet_id}")
        return env_sheet_id

    if not creds:
        print("❌ [GOOGLE SHEETS ERROR] Credenciais ausentes e SPREADSHEET_ID não definido. Desativando sincronização de planilha.")
        return None
    try:
        service = build("drive", "v3", credentials=creds)
        
        env_folder_id = os.environ.get("DRIVE_FOLDER_ID")
        folder_id = env_folder_id or folder_id or "1pjqOPcWHW8gCZ9GdLF7ta6NESN0dyw70"

        query = (
            f"name = 'Controle_Estoque_MercadoLivre' and '{folder_id}' in parents "
            f"and mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false"
        )
        results = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
        files = results.get('files', [])
        
        if files:
            sheet_id = files[0]['id']
            print(f"📄 Planilha 'Controle_Estoque_MercadoLivre' já existe. ID: {sheet_id}")
            
            sheets_service = build("sheets", "v4", credentials=creds)
            sheets_service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range="A1",
                valueInputOption="RAW",
                body={'values': [HEADERS]}
            ).execute()
        else:
            print("📄 Criando planilha 'Controle_Estoque_MercadoLivre'...")
            file_metadata = {
                'name': 'Controle_Estoque_MercadoLivre',
                'mimeType': 'application/vnd.google-apps.spreadsheet',
                'parents': [folder_id]
            }
            file = service.files().create(body=file_metadata, fields='id').execute()
            sheet_id = file.get('id')
            print(f"📄 Planilha criada com ID: {sheet_id}. Inserindo cabeçalhos...")
            
            sheets_service = build("sheets", "v4", credentials=creds)
            body = {
                'values': [HEADERS]
            }
            sheets_service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range="A1",
                valueInputOption="RAW",
                body=body
            ).execute()
            print("📄 Cabeçalhos inseridos com sucesso!")
            
        return sheet_id
        
    except Exception as error:
        import traceback
        print(f"❌ [GOOGLE SHEETS ERROR] Erro ao configurar a Planilha: {error}")
        traceback.print_exc()
        return None

def upload_product_photo(file_input, photos_folder_id, creds):
    """
    Faz upload de foto para o Google Drive com consumo de RAM reduzido (< 5MB).
    Retorna a URL pública do Google Drive ou None em caso de falha (sem gerar URLs mock fictícias).
    """
    if not creds or not photos_folder_id:
        print("❌ [GOOGLE DRIVE ERROR] Credenciais ou folder_id nulos. Upload cancelado (Nenhum link mock gerado).")
        return None

    random_id = uuid.uuid4().hex[:6]
    filename = f"foto_{format_brasilia_time('%Y%m%d_%H%M%S')}_{random_id}.jpg"

    temp_path = None
    media = None
    service = None
    created_temp_file = False

    try:
        if isinstance(file_input, str) and os.path.exists(file_input):
            temp_path = file_input
            filename = os.path.basename(file_input)
        else:
            from scripts.vision_processor import clean_and_decode_image_bytes
            clean_b = clean_and_decode_image_bytes(file_input)
            
            os.makedirs("temp_uploads", exist_ok=True)
            temp_path = os.path.join("temp_uploads", f"temp_upload_{random_id}.jpg")
            with open(temp_path, "wb") as f:
                f.write(clean_b)
            created_temp_file = True
            del clean_b
            gc.collect()

        print(f"📷 [GOOGLE DRIVE] Enviando '{filename}' via streaming leve (chunk=256KB, resumable=False)...")
        service = build("drive", "v3", credentials=creds)
        
        media = MediaFileUpload(
            temp_path, 
            mimetype='image/jpeg', 
            chunksize=256*1024, 
            resumable=False
        )
        
        file_metadata = {
            'name': filename,
            'parents': [photos_folder_id]
        }
        
        file_obj = service.files().create(
            body=file_metadata, 
            media_body=media, 
            fields='id, webViewLink'
        ).execute()
        
        file_id = file_obj.get('id')
        print(f"📷 [GOOGLE DRIVE SUCCESS] Upload concluído com sucesso! ID: {file_id}")
        
        try:
            permission = {'type': 'anyone', 'role': 'reader'}
            service.permissions().create(fileId=file_id, body=permission).execute()
        except Exception as perm_err:
            print(f"⚠️ Permissão pública no Drive: {perm_err}")

        public_url = file_obj.get('webViewLink') or f"https://drive.google.com/file/d/{file_id}/view"
        return public_url

    except Exception as error:
        import traceback
        print(f"❌ [GOOGLE DRIVE ERROR] Falha ao realizar upload para o Google Drive: {error}")
        traceback.print_exc()
        return None

    finally:
        del media, service
        if created_temp_file and temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        gc.collect()

def add_product_to_sheet(sheet_id, product_data, status, review_needed, review_reason, creds, product_id=""):
    """
    Adiciona uma nova linha com os dados de processamento do produto na planilha.
    """
    if not creds or not sheet_id or sheet_id == "mock_sheet_id":
        print(f"❌ [GOOGLE SHEETS ERROR] Credenciais ou planilha nulos. Ignorando gravação no Sheets para '{product_data.get('titulo')}'")
        return False
    try:
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
        
        sheets_service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range="A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body
        ).execute()
        print("📊 [GOOGLE SHEETS SYNC OK] Google Sheets Sync Concluído com Sucesso! Registro adicionado na planilha.")
        return True

        
    except Exception as e:
        import traceback
        print(f"❌ [GOOGLE SHEETS ERROR] Erro ao registrar produto na planilha: {e}")
        traceback.print_exc()
        return False


def delete_drive_file(file_url, creds):
    """
    Exclui um arquivo no Google Drive a partir da sua URL.
    """
    import re
    try:
        match = re.search(r'/file/d/([a-zA-Z0-9_-]+)', file_url) or re.search(r'id=([a-zA-Z0-9_-]+)', file_url)
        if not match:
            print(f"⚠️ Não foi possível extrair ID da URL: {file_url}")
            return False
            
        file_id = match.group(1)
        service = build("drive", "v3", credentials=creds)
        print(f"🗑️ Excluindo arquivo do Drive. ID: {file_id}")
        service.files().delete(fileId=file_id).execute()
        print(f"✅ Arquivo {file_id} excluído com sucesso do Drive.")
        return True
    except Exception as e:
        print(f"❌ Erro ao deletar arquivo do Drive: {e}")
        return False

def delete_sheet_row(sheet_id, row_num, creds):
    """
    Exclui uma linha específica na planilha Google Sheets.
    """
    try:
        sheets_service = build("sheets", "v4", credentials=creds)
        
        # Recupera o sheetId da primeira aba para garantir exatidão
        spreadsheet = sheets_service.spreadsheets().get(spreadsheetId=sheet_id).execute()
        sheets = spreadsheet.get("sheets", [])
        if not sheets:
            raise ValueError("Nenhuma aba encontrada na planilha.")
        grid_sheet_id = sheets[0]["properties"]["sheetId"]
        
        body = {
            "requests": [
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": grid_sheet_id,
                            "dimension": "ROWS",
                            "startIndex": row_num - 1,
                            "endIndex": row_num
                        }
                    }
                }
            ]
        }
        
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body=body
        ).execute()
        print(f"✅ Linha {row_num} excluída com sucesso do Sheets (sheetId: {grid_sheet_id}).")
        return True
    except Exception as e:
        print(f"❌ Erro ao excluir linha no Sheets: {e}")
        raise e

if __name__ == "__main__":
    folder_id = "1pjqOPcWHW8gCZ9GdLF7ta6NESN0dyw70"
    print("Iniciando teste de sincronização de Planilhas...")
    creds = authenticate(allow_interactive=True)
    
    # Configura e atualiza planilha
    sheet_id = setup_google_sheet(folder_id, creds)
    print(f"Pronto. Planilha ID: {sheet_id}")
