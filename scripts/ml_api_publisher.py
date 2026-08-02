# -*- coding: utf-8 -*-
"""
Mercado Livre API Publisher script.
"""
import json
import requests

class MLPublisher:
    def __init__(self, access_token=None, client_id=None, client_secret=None):
        """
        Inicializa o publicador do Mercado Livre com as credenciais da API.
        """
        self.access_token = access_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = "https://api.mercadolibre.com"

    def build_payload(self, product_data, image_urls):
        """
        Gera o payload JSON no formato exigido pela API do Mercado Livre (/items).
        """
        # Define um ID de categoria padrão se não for fornecido um código de categoria válido
        # MLB3530 é uma categoria genérica (ex: Outros)
        category_id = product_data.get("category_id", "MLB3530")
        
        # Mapeamento do formato de imagens para a API do ML
        pictures = [{"source": url} for url in image_urls]

        payload = {
            "title": product_data.get("titulo", ""),
            "category_id": category_id,
            "price": float(product_data.get("preco_sugerido", 0.0)),
            "currency_id": "BRL",
            "available_quantity": int(product_data.get("estoque", 1)),
            "buying_mode": "buy_it_now",
            "listing_type_id": product_data.get("listing_type_id", "gold_special"),
            "condition": product_data.get("condicao", "new"),
            "pictures": pictures,
            "status": "paused",
            "description": {
                "plain_text": product_data.get("descricao", "")
            }
        }
        return payload

    def validate_payload(self, payload):
        """
        Valida localmente se o payload contém todos os campos obrigatórios e se eles respeitam os limites da API.
        """
        errors = []
        
        # Validação do título
        title = payload.get("title", "")
        if not title:
            errors.append("O campo 'title' é obrigatório.")
        elif len(title) > 60:
            errors.append(f"O campo 'title' excede o limite de 60 caracteres (atual: {len(title)}).")
            
        # Validação da categoria
        if not payload.get("category_id"):
            errors.append("O campo 'category_id' é obrigatório.")
            
        # Validação de preço
        price = payload.get("price", 0.0)
        if price <= 0:
            errors.append("O campo 'price' deve ser maior que zero.")
            
        # Validação de estoque
        qty = payload.get("available_quantity", 0)
        if qty <= 0:
            errors.append("O campo 'available_quantity' deve ser pelo menos 1.")
            
        # Validação de fotos
        if not payload.get("pictures") or len(payload.get("pictures")) == 0:
            errors.append("Pelo menos uma imagem é necessária no array 'pictures'.")
            
        return len(errors) == 0, errors

    def publish_item(self, product_data, image_urls, dry_run=True):
        """
        Publica um produto no Mercado Livre. 
        Se dry_run=True, apenas simula e valida localmente sem realizar chamada HTTP real.
        """
        payload = self.build_payload(product_data, image_urls)
        
        # Realiza validação local dos campos
        is_valid, errors = self.validate_payload(payload)
        
        if dry_run:
            print("\n🔍 [MODO SIMULAÇÃO / DRY-RUN] Validando Payload...")
            print("Payload Gerado:")
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            
            if is_valid:
                print("\n✅ Validação local: Sucesso! Todos os campos obrigatórios estão corretos.")
                # Retorna resposta simulada de sucesso
                return {
                    "status": "success",
                    "dry_run": True,
                    "id": "MLB9999999999",
                    "permalink": "https://produto.mercadolivre.com.br/MLB-9999999999-produto-de-teste",
                    "payload": payload
                }
            else:
                print("\n❌ Validação local: Falha! Erros encontrados:")
                for err in errors:
                    print(f"   - {err}")
                return {
                    "status": "error",
                    "dry_run": True,
                    "errors": errors
                }
        else:
            if not self.access_token:
                raise ValueError("access_token não configurado. É necessário um token para publicação real.")
                
            if not is_valid:
                raise ValueError(f"Payload inválido para publicação real: {errors}")
                
            print("\n🚀 [MODO REAL] Publicando anúncio no Mercado Livre...")
            
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json"
            }
            
            # Nota: Na API real do ML, a descrição em formato plain_text às vezes precisa ser enviada
            # em um endpoint separado POST /items/{item_id}/description.
            # Removemos a descrição temporariamente do payload principal caso cause erro na criação do item.
            description_data = payload.pop("description", None)
            
            url = f"{self.base_url}/items"
            response = requests.post(url, json=payload, headers=headers)
            
            if response.status_code == 201:
                item_data = response.json()
                item_id = item_data.get("id")
                permalink = item_data.get("permalink")
                print(f"🎉 Anúncio criado com sucesso! ID: {item_id}")
                
                # Envia a descrição se houver
                if description_data and item_id:
                    desc_url = f"{self.base_url}/items/{item_id}/description"
                    desc_response = requests.post(desc_url, json=description_data, headers=headers)
                    if desc_response.status_code in (200, 201):
                        print("📝 Descrição do produto adicionada ao anúncio com sucesso.")
                    else:
                        print(f"⚠️ Erro ao adicionar descrição ao anúncio: {desc_response.text}")
                
                return {
                    "status": "success",
                    "dry_run": False,
                    "id": item_id,
                    "permalink": permalink,
                    "response": item_data
                }
            else:
                print(f"❌ Erro na publicação da API do Mercado Livre: {response.status_code} - {response.text}")
                return {
                    "status": "error",
                    "dry_run": False,
                    "code": response.status_code,
                    "message": response.text
                }

    def delete_item(self, item_id, dry_run=True):
        """
        Fecha e desativa um anúncio no Mercado Livre.
        Se dry_run=True, apenas simula a operação localmente.
        """
        if not item_id or item_id == "MLB9999999999" or not item_id.startswith("MLB"):
            print(f"ℹ️ ID do Mercado Livre inválido ou simulado ({item_id}). Ignorando exclusão real no ML.")
            return {"status": "success", "dry_run": True, "message": "ID simulado ignorado."}

        if dry_run:
            print(f"\n🔍 [MODO SIMULAÇÃO / DRY-RUN] Excluindo anúncio no Mercado Livre: {item_id}...")
            return {
                "status": "success",
                "dry_run": True,
                "id": item_id,
                "message": f"Anúncio {item_id} fechado e desativado (Simulado)."
            }
        else:
            if not self.access_token:
                raise ValueError("access_token não configurado. É necessário um token para alteração real.")
                
            print(f"\n🚀 [MODO REAL] Fechando e desativando anúncio {item_id} no Mercado Livre...")
            
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json"
            }
            
            # 1. Altera o status para closed (fechado)
            url = f"{self.base_url}/items/{item_id}"
            response = requests.put(url, json={"status": "closed"}, headers=headers)
            
            if response.status_code == 200:
                print(f"✅ Anúncio {item_id} alterado para status 'closed' com sucesso.")
                
                # 2. Tenta marcar como deletado
                try:
                    del_response = requests.put(url, json={"deleted": "true"}, headers=headers)
                    if del_response.status_code == 200:
                        print(f"✅ Anúncio {item_id} marcado como 'deleted' com sucesso.")
                    else:
                        print(f"⚠️ Não foi possível marcar como deletado (código {del_response.status_code}). Mantendo como 'closed'.")
                except Exception as del_err:
                    print(f"⚠️ Erro ao tentar marcar como deletado: {del_err}")
                
                return {
                    "status": "success",
                    "dry_run": False,
                    "id": item_id,
                    "message": f"Anúncio {item_id} fechado/deletado com sucesso."
                }
            else:
                print(f"❌ Erro ao fechar anúncio na API do Mercado Livre: {response.status_code} - {response.text}")
                return {
                    "status": "error",
                    "dry_run": False,
                    "code": response.status_code,
                    "message": response.text
                }

if __name__ == "__main__":
    print("Iniciando Módulo de Publicação do Mercado Livre (Teste)...")
    
    # 1. Dados fictícios obtidos do vision_processor
    mock_product_data = {
        "titulo": "Fone de Ouvido Bluetooth Sem Fio Premium DuoTech",
        "category_id": "MLB3530",
        "preco_sugerido": 159.90,
        "estoque": 2,
        "condicao": "new",
        "descricao": (
            "Fone de ouvido Bluetooth sem fio com excelente fidelidade sonora. "
            "Possui cancelamento de ruído passivo e bateria de longa duração."
        )
    }
    
    # 2. URLs fictícias geradas pelo drive_sheets_sync
    mock_image_urls = [
        "https://drive.google.com/file/d/1h5I35jfG7X7qnLbdS16iBFqdTAukPdkr/view?usp=drivesdk"
    ]
    
    # 3. Executa a publicação simulada (dry_run=True)
    publisher = MLPublisher(access_token="mock_token_12345")
    result = publisher.publish_item(mock_product_data, mock_image_urls, dry_run=True)
    
    print("\n📋 Resposta da Publicação Simulada:")
    print(json.dumps(result, indent=2, ensure_ascii=False))
