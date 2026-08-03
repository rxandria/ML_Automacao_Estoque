# -*- coding: utf-8 -*-
"""
Vision Processor script using Pillow and NumPy.
"""
import os
import re
import gc
import base64
import traceback


def clean_and_decode_image_bytes(raw_bytes):
    """
    Limpa e decodifica bytes de imagem recebidos.
    Se contiver cabeçalhos Data URL (ex: data:image/jpeg;base64,...) ou string em base64,
    remove o cabeçalho e decodifica para bytes binários puros de imagem.
    """
    if not raw_bytes:
        return b""
        
    try:
        # Tenta interpretar o cabeçalho como texto para identificar prefixos Data URL
        text_sample = raw_bytes[:120].decode('utf-8', errors='ignore').strip()
        
        if "base64," in text_sample or text_sample.startswith("data:image/"):
            if b"base64," in raw_bytes:
                _, base64_str = raw_bytes.split(b"base64,", 1)
            else:
                base64_str = raw_bytes
            
            base64_str = base64_str.strip()
            return base64.b64decode(base64_str)
            
        # Se for ASCII base64 puro (sem números mágicos de JPEG/PNG/GIF/WEBP)
        is_binary_header = (
            raw_bytes.startswith(b'\xff\xd8') or  # JPEG
            raw_bytes.startswith(b'\x89PNG') or  # PNG
            raw_bytes.startswith(b'GIF8') or     # GIF
            raw_bytes.startswith(b'RIFF')        # WEBP
        )
        if not is_binary_header:
            try:
                decoded = base64.b64decode(raw_bytes.strip())
                if decoded.startswith(b'\xff\xd8') or decoded.startswith(b'\x89PNG') or decoded.startswith(b'GIF8'):
                    return decoded
            except Exception:
                pass
    except Exception as e:
        print(f"⚠️ Aviso na higienização de bytes da imagem: {e}")
        
    return raw_bytes


def sanitize_title(title):
    """
    Higieniza o título garantindo que tenha no máximo 60 caracteres,
    removendo caracteres especiais indesejados e formatando em Title Case.
    """
    if not title:
        return ""
    sanitized = re.sub(r'[^a-zA-Z0-9 áéíóúâêîôûãõçÁÉÍÓÚÂÊÎÔÛÃÕÇ\-]', '', title)
    sanitized = re.sub(r'\s+', ' ', sanitized).strip()
    sanitized = sanitized.title()
    if len(sanitized) > 60:
        sanitized = sanitized[:57].strip() + "..."
    return sanitized


def optimize_image_for_ml(image_path, output_path, remove_bg=False):
    """
    Otimiza a imagem para os padrões exigidos pelo Mercado Livre:
    - Se remove_bg=True, remove o fundo original utilizando rembg.
    - Converte para RGB.
    - Redimensiona mantendo a proporção para 1200x1200px.
    - Adiciona fundo branco para centralizar a imagem.
    - Salva como JPEG na pasta de destino.
    """
    from PIL import Image
    try:
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"Imagem original não encontrada: {image_path}")

        # Sanitização de bytes de imagem caso o arquivo tenha chegado com cabeçalho Data URL ou Base64 ASCII
        try:
            with open(image_path, "rb") as f:
                raw_content = f.read()
            clean_bytes = clean_and_decode_image_bytes(raw_content)
            if clean_bytes != raw_content:
                print(f"🧹 Higienizando bytes de imagem Base64/DataURL em: {image_path}")
                with open(image_path, "wb") as f:
                    f.write(clean_bytes)
        except Exception as clean_err:
            print(f"⚠️ Erro ao higienizar imagem antes da otimização: {clean_err}")

        print(f"📷 Otimizando imagem: {image_path} (remove_bg={remove_bg})...")
        img = Image.open(image_path)
        
        # Se solicitado remover fundo
        if remove_bg:
            try:
                print("🧠 Removendo fundo da imagem com rembg...")
                max_ia_size = 1000
                w, h = img.size
                if w > max_ia_size or h > max_ia_size:
                    ratio = min(max_ia_size / w, max_ia_size / h)
                    img_for_ia = img.resize((int(w * ratio), int(h * ratio)), Image.Resampling.BILINEAR)
                    print(f"   Imagem reduzida de {w}x{h} para {img_for_ia.size[0]}x{img_for_ia.size[1]} para processamento de IA")
                else:
                    img_for_ia = img
                
                import signal
                class TimeoutException(Exception): pass
                def handler(signum, frame): raise TimeoutException("Timeout rembg")
                
                try:
                    signal.signal(signal.SIGALRM, handler)
                    signal.alarm(3)
                    has_alarm = True
                except ValueError:
                    has_alarm = False
                
                try:
                    from rembg import remove
                    img = remove(img_for_ia)
                finally:
                    if has_alarm:
                        signal.alarm(0)
            except Exception as e:
                print(f"⚠️ Erro ou timeout ao usar rembg ({e}). Continuando sem remoção de fundo...")
        
        # Se a imagem tiver canal alpha (RGBA/LA)
        if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
            target_size = 1200
            original_width, original_height = img.size
            ratio = min(target_size / original_width, target_size / original_height)
            new_width = int(original_width * ratio)
            new_height = int(original_height * ratio)
            img_resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
            
            new_img = Image.new("RGB", (target_size, target_size), (255, 255, 255))
            paste_x = (target_size - new_width) // 2
            paste_y = (target_size - new_height) // 2
            new_img.paste(img_resized, (paste_x, paste_y), img_resized)
        else:
            img = img.convert('RGB')
            target_size = 1200
            original_width, original_height = img.size
            ratio = min(target_size / original_width, target_size / original_height)
            new_width = int(original_width * ratio)
            new_height = int(original_height * ratio)
            img_resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
            
            new_img = Image.new("RGB", (target_size, target_size), (255, 255, 255))
            paste_x = (target_size - new_width) // 2
            paste_y = (target_size - new_height) // 2
            new_img.paste(img_resized, (paste_x, paste_y))
            
        output_dir = os.path.dirname(output_path)
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir)
            
        new_img.save(output_path, "JPEG", quality=90)
        print(f"📷 Imagem otimizada com sucesso em: {output_path} (1200x1200px)")
        
        try:
            img.close()
            new_img.close()
        except Exception:
            pass
        return output_path
        
    except Exception as e:
        print(f"❌ Erro ao otimizar a imagem: {e}")
        traceback.print_exc()
        gc.collect()
        raise e
    finally:
        gc.collect()

def analyze_product_image(image_path):
    """
    Extrai dados do produto a partir da imagem com regras de validação e resiliência:
    - Sanitiza bytes base64/DataURL.
    - Verifica tamanho mínimo (500x500px).
    - Avalia nitidez/contraste usando desvio padrão de tons de cinza.
    - Em caso de falha na IA/Gemini, adiciona fallback e marca para revisão manual.
    """
    from PIL import Image
    import numpy as np
    
    print(f"🧠 Analisando imagem {image_path}...")
    try:
        # Sanitização de bytes de imagem caso o arquivo tenha chegado com cabeçalho Data URL ou Base64 ASCII
        if os.path.exists(image_path):
            try:
                with open(image_path, "rb") as f:
                    raw_content = f.read()
                clean_bytes = clean_and_decode_image_bytes(raw_content)
                if clean_bytes != raw_content:
                    print(f"🧹 Higienizando bytes de imagem Base64/DataURL em: {image_path}")
                    with open(image_path, "wb") as f:
                        f.write(clean_bytes)
            except Exception as clean_err:
                print(f"⚠️ Erro ao higienizar imagem antes da análise: {clean_err}")

        img = Image.open(image_path)
        width, height = img.size
        
        # Calcula nitidez/contraste usando o desvio padrão de tons de cinza do NumPy
        gray_img = img.convert('L')
        std_dev = float(np.std(np.array(gray_img)))

        confidence_score = 95.0
        requires_manual_review = False
        review_reason = ""
        
        # Verificações de qualidade de imagem
        if width < 500 or height < 500:
            requires_manual_review = True
            review_reason += f"Dimensões baixas ({width}x{height}px). Mínimo recomendado: 500x500px. "
            confidence_score -= 30
            
        if std_dev < 15.0:
            requires_manual_review = True
            review_reason += f"Imagem com baixa nitidez ou contraste (DevPad: {std_dev:.2f}). "
            confidence_score -= 25
            
        # Conexão Real da IA com Gemini Visão
        import io
        import base64
        import json
        import requests
        
        try:
            api_key = os.environ.get("GEMINI_API_KEY")
            
            if not api_key:
                env_paths = [
                    ".env",
                    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
                    "../.env"
                ]
                for env_path in env_paths:
                    if os.path.exists(env_path):
                        try:
                            with open(env_path, "r", encoding="utf-8") as f:
                                for line in f:
                                    if line.strip().startswith("GEMINI_API_KEY="):
                                        api_key = line.strip().split("=", 1)[1].strip('"\' ')
                                        break
                        except Exception as env_err:
                            print(f"⚠️ Erro ao ler arquivo {env_path}: {env_err}")
                    if api_key:
                        break
            
            if not api_key:
                raise ValueError(
                    "Chave de API do Gemini (GEMINI_API_KEY) não encontrada. "
                    "Configure a variável de ambiente GEMINI_API_KEY ou crie um arquivo .env."
                )
                
            print("🔗 Conectando à API do Gemini Visão para análise real...")
            
            # Prepara a imagem em base64 limpa
            buffered = io.BytesIO()
            img_to_send = img.convert("RGB")
            img_to_send.save(buffered, format="JPEG", quality=85)
            raw_img_bytes = buffered.getvalue()
            img_base64 = base64.b64encode(raw_img_bytes).decode('utf-8')
            img_base64 = re.sub(r'^data:image/[^;]+;base64,', '', img_base64).strip()
            
            # Fecha a imagem convertida auxiliar e limpa o buffer
            img_to_send.close()
            buffered.close()
            del raw_img_bytes, img_to_send, buffered
            gc.collect()

            gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
            
            prompt = (
                "Analise a imagem deste produto. Realize OCR de etiquetas, "
                "identifique marcas, modelos (ex: caixas de som JBL, cabos, componentes, etc.), "
                "Part Numbers (PN, SPARE PN) e descrição técnica. "
                "Retorne os dados em formato JSON estrito com as chaves: "
                '"titulo" (comercial e específico, máximo 60 caracteres), '
                '"categoria" (categoria apropriada do Mercado Livre), '
                '"preco_sugerido" (número), "condicao" ("new" ou "used"), '
                '"descricao" (descrição comercial detalhada). '
                "Retorne APENAS o JSON puro, sem blocos de markdown ```json."
            )
            
            payload = {
                "contents": [
                    {
                        "parts": [
                            {"text": prompt},
                            {
                                "inlineData": {
                                    "mimeType": "image/jpeg",
                                    "data": img_base64
                                }
                            }
                        ]
                    }
                ],
                "generationConfig": {
                    "responseMimeType": "application/json"
                }
            }
            
            headers = {"Content-Type": "application/json"}
            response = requests.post(gemini_url, json=payload, headers=headers, timeout=15)
            
            # Limpa imediatamente o buffer base64 da memória após o envio da requisição
            del img_base64, payload
            gc.collect()

            if response.status_code != 200:
                raise RuntimeError(f"Erro ao chamar API do Gemini ({response.status_code}): {response.text}")
                
            resp_json = response.json()
            del response
            gc.collect()

            try:
                raw_text = resp_json['candidates'][0]['content']['parts'][0]['text'].strip()
            except (KeyError, IndexError) as err:
                raise RuntimeError(f"Resposta inválida da API do Gemini: {resp_json}") from err
                
            if raw_text.startswith("```"):
                lines = raw_text.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines[-1].startswith("```"):
                    lines = lines[:-1]
                raw_text = "\n".join(lines).strip()
                
            try:
                data = json.loads(raw_text)
            except Exception as json_err:
                raise RuntimeError(f"Falha ao decodificar JSON retornado pelo Gemini: {raw_text}") from json_err
                
            titulo = data.get("titulo")
            categoria = data.get("categoria")
            preco = float(data.get("preco_sugerido", 99.90))
            condicao = data.get("condicao", "new")
            descricao = data.get("descricao", "")
            print("✅ Resposta da API do Gemini processada com sucesso!")
            
        except Exception as e:
            print(f"⚠️ Erro ao chamar a API do Gemini Visão: {e}")
            traceback.print_exc()
            requires_manual_review = True
            review_reason += f"Falha na API do Gemini: {str(e)}. "
            confidence_score = 0.0

            
            filename = os.path.basename(image_path).lower() if image_path else ""
            if "fone" in filename:
                titulo = "Fone de Ouvido (Revisar Modelo)"
                categoria = "Eletrônicos, Áudio e Vídeo > Áudio > Fones de Ouvido"
                preco = 99.90
                descricao = "Fone de ouvido Bluetooth sem fio. Por favor, revise a marca, modelo e detalhes técnicos."
            elif "jbl" in filename or "caixa" in filename or "som" in filename:
                titulo = "Caixa de Som (Revisar Marca/Modelo)"
                categoria = "Eletrônicos, Áudio e Vídeo > Áudio > Caixas de Som"
                preco = 199.90
                descricao = "Caixa de som / Alto-falante. Por favor, revise e insira a marca e modelo corretos."
            else:
                titulo = "Produto Desconhecido (Revisar Cadastro)"
                categoria = "Outros"
                preco = 50.00
                descricao = f"Erro na análise automática: {str(e)}. Por favor, preencha manualmente os dados."
            condicao = "used"
        
        sanitized_title = sanitize_title(titulo)
        
        if not sanitized_title or len(sanitized_title) < 5 or "..." in sanitized_title:
            requires_manual_review = True
            review_reason += "Título inválido ou truncado. "
            confidence_score -= 15
            
        if not categoria:
            requires_manual_review = True
            review_reason += "Categoria ausente. "
            confidence_score -= 10
            
        if preco <= 0:
            requires_manual_review = True
            review_reason += f"Preço inválido ({preco}). "
            confidence_score -= 20

        confidence_score = max(0.0, min(100.0, confidence_score))
        
        return {
            "titulo": sanitized_title,
            "categoria": categoria,
            "preco_sugerido": preco,
            "estoque": 1,
            "condicao": condicao,
            "descricao": descricao,
            "confidence_score": f"{confidence_score:.1f}%",
            "requires_manual_review": requires_manual_review,
            "review_reason": review_reason.strip()
        }
        
    except Exception as e:
        print(f"❌ [VISION PROCESSOR EXCEPTION] Erro crítico ao analisar imagem '{image_path}': {e}")
        traceback.print_exc()
        gc.collect()
        
        filename = os.path.basename(image_path).lower() if image_path else "produto"
        raw_name = os.path.splitext(filename)[0]
        
        return {
            "titulo": sanitize_title(f"Produto {raw_name.replace('_', ' ').title()} (Revisar Foto)"),
            "categoria": "Outros",
            "preco_sugerido": 50.00,
            "estoque": 1,
            "condicao": "used",
            "descricao": f"Erro de leitura na imagem: {str(e)}. Necessário preenchimento manual.",
            "confidence_score": "0.0%",
            "requires_manual_review": True,
            "review_reason": f"Falha de leitura/processamento da imagem ({str(e)}). Necessária revisão manual."
        }

    finally:
        try:
            img.close()
        except Exception:
            pass
        gc.collect()


if __name__ == "__main__":
    print("Testando higienização de títulos:")
    test_titles = [
        "FONE DE OUVIDO!!! @Bluetooth #DuoTech 2026",
        "Este título de produto é extremamente longo e vai acabar sendo truncado pela função de higienização"
    ]
    for t in test_titles:
        print(f"  Antes: '{t}' -> Depois: '{sanitize_title(t)}'")
        
    print("\nExecutando análise de imagem normal:")
    try:
        res_ok = analyze_product_image("temp_uploads/test_product.jpg")
        print(f"  Requires review: {res_ok['requires_manual_review']} (Motivo: '{res_ok['review_reason']}')")
    except Exception as e:
        print(f"  Falha na análise (esperada se não houver chave de API): {e}")
    
    print("\nExecutando análise de imagem com falha (Simulado):")
    try:
        res_fail = analyze_product_image("temp_uploads/test_revisar.jpg")
        print(f"  Requires review: {res_fail['requires_manual_review']} (Motivo: '{res_fail['review_reason']}')")
    except Exception as e:
        print(f"  Falha na análise (esperada se não houver chave de API): {e}")
