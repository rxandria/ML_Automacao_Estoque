# -*- coding: utf-8 -*-
"""
Google Drive & Sheets synchronization script.
"""
import os.path
import mimetypes
import datetime
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

# Cabeçalhos da planilha
HEADERS = [
    "ID Produto", "Título", "Categoria", "Preço (R$)", 
    "Estoque", "Condição", "URL Fotos", "Status ML", 
    "Revisão Necessária", "Motivo Revisão", "Data Criação"
]

def authenticate(allow_interactive=False):
    creds = None
    import traceback
    
    # 1. Tenta carregar as credenciais a partir da variável de ambiente em produção
    google_token_json = os.environ.get("GOOGLE_TOKEN_JSON")
    if google_token_json:
        try:
            import json
            token_data = json.loads(google_token_json)
            creds = Credentials.from_authorized_user_info(token_data, SCOPES)
            print("🔑 Credenciais do Google carregadas com sucesso de GOOGLE_TOKEN_JSON.")
        except Exception as e:
            print(f"⚠️ [GOOGLE AUTH ERROR] Erro ao decodificar/carregar GOOGLE_TOKEN_JSON: {e}")
            traceback.print_exc()
            creds = None
            
    # 2. Fallback para carregar do arquivo local token.json
    if not creds and os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            print("🔑 Credenciais do Google carregadas com sucesso do token.json local.")
        except Exception as e:
            print(f"⚠️ [GOOGLE AUTH ERROR] Erro ao carregar token.json local: {e}")
            traceback.print_exc()
            creds = None
            
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                print("🔄 Token de acesso do Google renovado com sucesso.")
            except Exception as e:
                print(f"⚠️ [GOOGLE AUTH ERROR] Erro ao renovar token do Google: {e}")
                traceback.print_exc()
                creds = None
                
        if not creds or not creds.valid:
            if not allow_interactive:
                print("⚠️ [GOOGLE AUTH WARNING] Credenciais do Google nulas, expiradas ou ausentes em ambiente não interativo.")
                return None
            
            # Se for interativo, tenta carregar credentials
            google_credentials_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
            if google_credentials_json:
                try:
                    import json
                    client_config = json.loads(google_credentials_json)
                    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
                except Exception as e:
                    print(f"⚠️ Erro ao carregar GOOGLE_CREDENTIALS_JSON: {e}")
                    traceback.print_exc()
                    flow = None
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    CREDENTIALS_FILE, SCOPES
                )
                
            creds = flow.run_local_server(port=0)
            
        # Tenta salvar localmente se não for produção
        try:
            with open(TOKEN_FILE, "w") as token:
                token.write(creds.to_json())
        except Exception as e:
            print(f"⚠️ Erro ao salvar token.json localmente: {e}")
            
    return creds

def setup_drive_structure(folder_id, creds):
    """
    Verifica se a pasta 'Fotos_Produtos' existe dentro de folder_id.
    Se não existir, cria e retorna o ID.
    """
    if not creds:
        print("⚠️ [GOOGLE DRIVE] Credenciais ausentes. Retornando folder_id simulado.")
        return folder_id or "mock_photos_folder_id"
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
        return folder_id or "mock_photos_folder_id"

def setup_google_sheet(folder_id, creds):
    """
    Verifica se a planilha 'Controle_Estoque_MercadoLivre' existe na pasta.
    Se não existir, cria e insere os cabeçalhos atualizados.
    """
    if not creds:
        print("⚠️ [GOOGLE SHEETS] Credenciais ausentes. Retornando sheet_id simulado.")
        return "mock_sheet_id"
    try:
        service = build("drive", "v3", credentials=creds)
        
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
        return "mock_sheet_id"

def upload_product_photo(file_path, photos_folder_id, creds):
    """
    Faz upload de imagem local para a pasta Fotos_Produtos e retorna link público.
    """
    fallback_url = f"https://drive.google.com/file/d/mock_{os.path.basename(file_path)}/view"
    if not creds:
        print(f"⚠️ [GOOGLE DRIVE] Credenciais ausentes. Retornando URL simulada para '{os.path.basename(file_path)}'.")
        return fallback_url
    try:
        service = build("drive", "v3", credentials=creds)
        
        filename = os.path.basename(file_path)
        mime_type, _ = mimetypes.guess_type(file_path)
        if not mime_type:
            mime_type = 'application/octet-stream'
            
        print(f"📷 Fazendo upload de '{filename}' ({mime_type})...")
        
        file_metadata = {
            'name': filename,
            'parents': [photos_folder_id]
        }
        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
        
        file = service.files().create(
            body=file_metadata, 
            media_body=media, 
            fields='id, webViewLink'
        ).execute()
        
        file_id = file.get('id')
        print(f"📷 Upload concluído! ID do arquivo: {file_id}")
        
        print("📷 Definindo permissões como pública para visualização...")
        permission = {
            'type': 'anyone',
            'role': 'reader'
        }
        service.permissions().create(fileId=file_id, body=permission).execute()
        
        file_info = service.files().get(fileId=file_id, fields='webViewLink').execute()
        public_url = file_info.get('webViewLink', fallback_url)
        
        return public_url
        
    except Exception as error:
        import traceback
        print(f"❌ [GOOGLE DRIVE ERROR] Erro ao fazer upload do arquivo '{file_path}': {error}")
        traceback.print_exc()
        return fallback_url

def add_product_to_sheet(sheet_id, product_data, status, review_needed, review_reason, creds, product_id=""):
    """
    Adiciona uma nova linha com os dados de processamento do produto na planilha.
    Formatos de status permitidos: "PENDENTE", "REVISAO_MANUAL", "HOMOLOGADO (DRY RUN)", "PUBLICADO", "ERRO".
    """
    if not creds or sheet_id == "mock_sheet_id":
        print(f"⚠️ [GOOGLE SHEETS] Credenciais nulas ou planilha simulada. Ignorando gravação remota no Sheets para '{product_data.get('titulo')}'")
        return False
    try:
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
        
        sheets_service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range="A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body
        ).execute()
        print("📊 Registro do produto adicionado na Planilha do Google Sheets!")
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
