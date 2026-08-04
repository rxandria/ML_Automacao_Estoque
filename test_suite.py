# -*- coding: utf-8 -*-
"""
Suíte de Testes e Validação Integrada para ML_Automacao_Estoque.
Testa:
1. Higienização de Env Vars e Carregamento de Auth.
2. Formatação de Range do Google Sheets & Safe Wrappers com Fallback de HttpError 404.
3. Processamento de Imagem (Canvas 1200x1200px #FFFFFF).
4. Integração do Pipeline run_pipeline / API Upload.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch
from googleapiclient.errors import HttpError
from httplib2 import Response
from PIL import Image

# Inclui o diretório raiz no PATH
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scripts.drive_sheets_sync import (
    get_sanitized_env,
    format_sheet_range,
    get_first_sheet_name,
    safe_sheets_get,
    safe_sheets_update,
    safe_sheets_append
)
from scripts.vision_processor import optimize_image_for_ml
import main


class TestCloudRunResilience(unittest.TestCase):

    def test_01_env_var_sanitization(self):
        print("\n🧪 [TEST 1] Testando higienização de variáveis de ambiente...")
        with patch.dict(os.environ, {
            "SPREADSHEET_ID": "  11AABBCC_Sheet_Id_With_Newlines\n\n ",
            "DRIVE_FOLDER_ID": "'11XXYYZZ_Folder_Id_Quoted' ",
            "GOOGLE_CREDENTIALS_JSON": '{"type": "service_account"} \n'
        }):
            self.assertEqual(get_sanitized_env("SPREADSHEET_ID"), "11AABBCC_Sheet_Id_With_Newlines")
            self.assertEqual(get_sanitized_env("DRIVE_FOLDER_ID"), "11XXYYZZ_Folder_Id_Quoted")
            self.assertEqual(get_sanitized_env("GOOGLE_CREDENTIALS_JSON"), '{"type": "service_account"}')
        print("✅ [TEST 1 PASSED] Higienização de Env Vars operando 100% com .strip()!")

    def test_02_range_formatting_and_404_fallback(self):
        print("\n🧪 [TEST 2] Testando formatação de ranges e fallback de HttpError 404...")
        # 2a. Teste da formatação de range
        self.assertEqual(format_sheet_range("Sheet1", "A2:K200"), "Sheet1!A2:K200")
        self.assertEqual(format_sheet_range("Página 1", "A2:K200"), "'Página 1'!A2:K200")
        self.assertEqual(format_sheet_range("", "A2:K200"), "A2:K200")

        # 2b. Mock de service do Google Sheets simulando HttpError 404 no range primário e sucesso no fallback
        resp_404 = Response({'status': '404', 'reason': 'Not Found'})
        err_404 = HttpError(resp_404, b'{"error": {"code": 404, "message": "Requested entity was not found."}}')

        mock_values = MagicMock()
        mock_get_primary = MagicMock()
        mock_get_primary.execute.side_effect = err_404

        mock_get_fallback = MagicMock()
        mock_get_fallback.execute.return_value = {"values": [["HEADER"], ["P1"]]}

        def mock_get_side_effect(spreadsheetId, range):
            if "Sheet1!" in range or "'Sheet1'!" in range:
                return mock_get_primary
            return mock_get_fallback

        mock_values.get.side_effect = mock_get_side_effect

        mock_sheets_service = MagicMock()
        mock_sheets_service.spreadsheets.return_value.values.return_value = mock_values

        result = safe_sheets_get(mock_sheets_service, "dummy_sheet_id", "Sheet1", "A2:K200")
        self.assertIn("values", result)
        self.assertEqual(result["values"][1][0], "P1")
        print("✅ [TEST 2 PASSED] Safe Wrappers e Fallback de HttpError 404 validados com sucesso!")

    def test_03_image_processing_1200x1200_white_bg(self):
        print("\n🧪 [TEST 3] Testando otimização de imagem para 1200x1200px com fundo #FFFFFF...")
        os.makedirs("temp_uploads", exist_ok=True)
        test_img_path = os.path.join("temp_uploads", "unit_test_img.jpg")
        output_img_path = os.path.join("temp_uploads", "unit_test_img_out.jpg")

        # Cria imagem de teste 400x300 azul
        img = Image.new("RGB", (400, 300), (0, 102, 204))
        img.save(test_img_path)

        res_path = optimize_image_for_ml(test_img_path, output_path=output_img_path, remove_bg=False)
        self.assertTrue(os.path.exists(res_path))

        with Image.open(res_path) as out_img:
            self.assertEqual(out_img.size, (1200, 1200))
            self.assertEqual(out_img.mode, "RGB")
            # Verifica a cor do canto superior esquerdo (deve ser #FFFFFF / 255, 255, 255)
            corner_pixel = out_img.getpixel((0, 0))
            self.assertEqual(corner_pixel, (255, 255, 255))

        print("✅ [TEST 3 PASSED] Imagem processada e validada em 1200x1200px com Fundo Branco Puro (#FFFFFF)!")

    def test_04_pipeline_integration(self):
        print("\n🧪 [TEST 4] Testando integração do pipeline run_pipeline...")
        os.makedirs("temp_uploads", exist_ok=True)
        test_img_path = os.path.join("temp_uploads", "unit_test_img.jpg")

        res = main.run_pipeline([test_img_path], dry_run=True, product_id="prod_test_123")
        self.assertIsInstance(res, dict)
        self.assertTrue("requires_manual_review" in res or "success" in res)
        # Verifica se o produto foi registrado no cache local com o product_id especificado
        saved_prod = next((p for p in main.LOCAL_PRODUCTS if p.get("id") == "prod_test_123"), None)
        self.assertIsNotNone(saved_prod)
        self.assertEqual(saved_prod["id"], "prod_test_123")
        print("✅ [TEST 4 PASSED] Pipeline de cadastros e triagem executado com sucesso sem duplicação!")

    def test_05_auto_provisioning(self):
        print("\n🧪 [TEST 5] Testando Auto-Provisionamento de Planilha e Pasta no Drive...")
        from scripts.drive_sheets_sync import (
            auto_create_drive_folder,
            auto_create_spreadsheet,
            get_or_create_drive_folder_id,
            get_or_create_spreadsheet_id
        )

        mock_creds = MagicMock()
        with patch("scripts.drive_sheets_sync.build") as mock_build:
            # Mock Drive & Sheets API responses
            mock_drive_service = MagicMock()
            mock_sheets_service = MagicMock()

            def build_side_effect(service_name, version, credentials):
                if service_name == "drive":
                    return mock_drive_service
                return mock_sheets_service

            mock_build.side_effect = build_side_effect
            
            # Simula criacao de arquivo
            mock_drive_service.files().create().execute.side_effect = [
                {"id": "auto_folder_123"},
                {"id": "auto_sheet_456"}
            ]

            folder_id = auto_create_drive_folder(mock_creds)
            self.assertEqual(folder_id, "auto_folder_123")

            sheet_id = auto_create_spreadsheet(mock_creds, folder_id=folder_id)
            self.assertEqual(sheet_id, "auto_sheet_456")

            # Garanta que get_or_create_spreadsheet_id / get_or_create_drive_folder_id retornem os novos IDs
            self.assertEqual(get_or_create_drive_folder_id(mock_creds), "auto_folder_123")
            self.assertEqual(get_or_create_spreadsheet_id(mock_creds), "auto_sheet_456")

        print("✅ [TEST 5 PASSED] Auto-Provisionamento de Planilha e Pasta no Drive validado 100%!")


if __name__ == "__main__":
    unittest.main()
