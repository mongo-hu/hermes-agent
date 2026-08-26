import os
import sys
import json

# =====================================================================
# 1. LLM ENGINE (Map-Reduce 语义总结)
# =====================================================================
from pydantic import BaseModel, Field
from openai import OpenAI

class DfmExtraction(BaseModel):
    Material: str = Field(description="材料 (e.g., ABS, PC, Steel)", default="null")
    General_Tolerance: str = Field(description="通用公差 (e.g., DIN ISO 2768-m)", default="null")
    Surface_Finish: str = Field(description="表面粗糙度 (e.g., Ra 1.6)", default="null")
    Part_Name: str = Field(description="零件名称", default="null")
    Manufacturing_Constraints: list[str] = Field(description="制造约束或工艺限制", default_factory=list)
    Thread_Requirements: list[str] = Field(description="螺纹或紧固件要求", default_factory=list)
    Other_Global_Notes: list[str] = Field(description="其他备注", default_factory=list)

def run_extraction(raw_text: str, model_name: str = "gpt-4o") -> dict:
    if not raw_text or not raw_text.strip(): return {}
    api_key = os.environ.get("OPENAI_API_KEY", "EMPTY")
    
    if api_key == "EMPTY" or not api_key:
        # 当没有 API Key 时，直接返回纯文本，交由我在对话框中作为 LLM 进行总结
        return {
            "Status": "Waiting for Antigravity LLM Summarization",
            "Raw_Extracted_Text": raw_text
        }
        
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    client = OpenAI(api_key=api_key, base_url=base_url)
    
    system_prompt = "你是资深机械工程师，请分析提取散乱的 CAD 图纸文本中的核心工艺和制造约束。"
    try:
        response = client.beta.chat.completions.parse(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"图纸提取出的纯文本如下:\n\n{raw_text[:8000]}"}
            ],
            response_format=DfmExtraction,
            temperature=0.1
        )
        return response.choices[0].message.parsed.model_dump()
    except Exception as e:
        return {"error": str(e)}


# =====================================================================
# 2. IMAGE & PDF ENGINE (统一走 RapidOCR 降维提取)
# =====================================================================
try:
    from rapidocr_onnxruntime import RapidOCR
    import fitz
    HAS_OCR = True
    _READER = None
except ImportError:
    HAS_OCR = False
    _READER = None

def get_ocr_engine():
    global _READER
    if _READER is None and HAS_OCR:
        _READER = RapidOCR()
    return _READER

def extract_img_text(img_path: str) -> str:
    reader = get_ocr_engine()
    if not reader: return ""
    result, _ = reader(img_path)
    return "\n".join([line[1] for line in result]) if result else ""

def extract_pdf_text(pdf_path: str) -> str:
    reader = get_ocr_engine()
    if not reader: return ""
    ocr_text = ""
    with fitz.open(pdf_path) as doc:
        for page in doc:
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # 2倍超分渲染
            result, _ = reader(pix.tobytes("png"))           # 直接走OCR引擎
            if result:
                ocr_text += "\n".join([line[1] for line in result]) + "\n"
    return ocr_text


# =====================================================================
# 3. DWG NATIVE ENGINE (原生提取 - 单图模式)
# =====================================================================
try:
    import aspose.cad as cad
    from aspose.pycore import cast
    HAS_ASPOSE = True
except ImportError:
    HAS_ASPOSE = False

def extract_dwg_text(file_path: str) -> str:
    """按新需求，全量提取 DWG 文本，不再做多图物理切片分割"""
    if not HAS_ASPOSE: return ""
    image = cad.Image.load(file_path)
    texts = []
    
    def _extract_text_from_entity(entity):
        type_name = getattr(entity, 'type_name', None)
        if type_name in [cad.fileformats.cad.cadconsts.CadEntityTypeName.TEXT, 
                         cad.fileformats.cad.cadconsts.CadEntityTypeName.MTEXT]:
            try:
                text_entity = cast(cad.fileformats.cad.cadobjects.CadText, entity)
                val = text_entity.default_value or getattr(text_entity, 'text', '')
                if val and len(val.strip()) > 1:
                    texts.append(val.strip().replace('\n', ' '))
            except: pass

    with cast(cad.fileformats.cad.CadImage, image) as cad_image:
        if hasattr(cad_image, 'entities'):
            for entity in cad_image.entities:
                _extract_text_from_entity(entity)
        if hasattr(cad_image, 'block_entities'):
            for block in getattr(cad_image.block_entities, 'values', cad_image.block_entities):
                try:
                    for entity in block.entities:
                        _extract_text_from_entity(entity)
                except: pass
                
    return "\n".join(texts)


# =====================================================================
# 4. UNIFIED PIPELINE ROUTER (统一调度网关)
# =====================================================================
def process_file(file_path: str, quiet: bool = False):
    ext = os.path.splitext(file_path)[1].lower()
    raw_text = ""
    
    # 策略 1：图片走 OCR
    if ext in [".png", ".jpg", ".bmp"]:
        if not quiet: print(f"-> [路由命中] Image OCR Pipeline: {file_path}")
        raw_text = extract_img_text(file_path)
        
    # 策略 2：PDF 转图强制走 OCR
    elif ext == ".pdf":
        if not quiet: print(f"-> [路由命中] PDF(转图) -> OCR Pipeline: {file_path}")
        raw_text = extract_pdf_text(file_path)
        
    # 策略 3：DWG 原生文本提取 (单图模式)
    elif ext == ".dwg":
        if not quiet: print(f"-> [路由命中] DWG 原生文本提取 (单图模式): {file_path}")
        raw_text = extract_dwg_text(file_path)
        
    else:
        if not quiet: print(f"不支持的文件格式: {ext}")
        return {}, ""

    # 不在黑盒内部写文件，把 raw_text 随结果一起向上抛出
    return {"Global_Drawing": run_extraction(raw_text)}, raw_text

if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = sys.argv[1]
        print("\n=== M3 Unified 2D Pipeline ===")
        final_json, _ = process_file(target)
        print(json.dumps(final_json, indent=2, ensure_ascii=False))
