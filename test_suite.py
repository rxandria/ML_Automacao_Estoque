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
    get_sanitized_env
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
    def test_05_upload_photo_shared_drive(self):
        print("\n🧪 [TEST 5] Testando upload para pasta pai no Drive com supportsAllDrives=True...")
        from scripts.drive_sheets_sync import upload_product_photo

        mock_creds = MagicMock()
        test_file_path = os.path.join("temp_uploads", "unit_test_img.jpg")

        with patch("scripts.drive_sheets_sync.build") as mock_build:
            mock_service = MagicMock()
            mock_build.return_value = mock_service

            mock_create = MagicMock()
            mock_create.execute.return_value = {
                "id": "file_123_abc",
                "webViewLink": "https://drive.google.com/file/d/file_123_abc/view"
            }
            mock_service.files().create.return_value = mock_create

            url = upload_product_photo(test_file_path, "  folder_target_456  ", mock_creds)

            mock_service.files().create.assert_called_once()
            call_kwargs = mock_service.files().create.call_args[1]
            self.assertTrue(call_kwargs.get("supportsAllDrives"))
            self.assertEqual(call_kwargs.get("body", {}).get("parents"), ["folder_target_456"])
            self.assertEqual(url, "https://drive.google.com/file/d/file_123_abc/view")

        print("✅ [TEST 5 PASSED] Upload para pasta compartilhada validado com parents e supportsAllDrives=True!")

if __name__ == "__main__":
    unittest.main()
