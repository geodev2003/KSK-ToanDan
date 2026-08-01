# -*- coding: utf-8 -*-
"""
Sinh PHIẾU KHÁM SỨC KHỎE để in cho từng bệnh nhân, dựa theo cấu trúc
"Mẫu giấy khám sức khỏe" của Bộ Y tế (phần THÔNG TIN HÀNH CHÍNH lấy từ dữ liệu
đã nhập trong app; phần KẾT QUẢ KHÁM để trống cho bác sĩ điền tay khi khám).

Lưu ý: đây là bản rút gọn dùng chung cho nhiều lứa tuổi (bỏ các mục chuyên biệt
cho trẻ 6–18 tuổi như tiêm chủng / sàng lọc tăng động, tự kỷ). Nếu đơn vị cần
đúng 100% theo Mẫu 01 (người lớn) hoặc Mẫu 02 (trẻ em) của Thông tư hiện hành,
nên đối chiếu lại và điều chỉnh thêm.
"""
import io
import os
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, KeepTogether, Image
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ---------------- font tiếng Việt Times New Roman ----------------
_APP_DIR = os.path.dirname(__file__)
_ROOT_DIR = os.path.dirname(_APP_DIR)

_TTF_REG = os.path.join(_APP_DIR, "fonts", "TimesNewRoman.ttf")
_TTF_BOLD = os.path.join(_APP_DIR, "fonts", "TimesNewRoman-Bold.ttf")

if os.path.exists(_TTF_REG) and os.path.exists(_TTF_BOLD):
    pdfmetrics.registerFont(TTFont("VN", _TTF_REG))
    pdfmetrics.registerFont(TTFont("VN-Bold", _TTF_BOLD))
    FONT, FONT_BOLD = "VN", "VN-Bold"
else:
    _FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/share/fonts/TTF"]
    _REGULAR = _BOLD = None
    for d in _FONT_DIRS:
        r, b = os.path.join(d, "DejaVuSans.ttf"), os.path.join(d, "DejaVuSans-Bold.ttf")
        if os.path.exists(r) and os.path.exists(b):
            _REGULAR, _BOLD = r, b
            break
    if _REGULAR:
        pdfmetrics.registerFont(TTFont("VN", _REGULAR))
        pdfmetrics.registerFont(TTFont("VN-Bold", _BOLD))
        FONT, FONT_BOLD = "VN", "VN-Bold"
    else:
        FONT, FONT_BOLD = "Helvetica", "Helvetica-Bold"

BLANK = "…" * 3


def _styles():
    return {
        "title": ParagraphStyle("title", fontName=FONT_BOLD, fontSize=12, leading=15, alignment=TA_CENTER),
        "sub": ParagraphStyle("sub", fontName=FONT, fontSize=10, leading=13, alignment=TA_CENTER),
        "h2": ParagraphStyle("h2", fontName=FONT_BOLD, fontSize=11, leading=14, spaceBefore=8, spaceAfter=4),
        "h3": ParagraphStyle("h3", fontName=FONT_BOLD, fontSize=10, leading=13, spaceBefore=4),
        "normal": ParagraphStyle("normal", fontName=FONT, fontSize=10, leading=14),
        "small": ParagraphStyle("small", fontName=FONT, fontSize=8.5, leading=11),
        "center": ParagraphStyle("center", fontName=FONT, fontSize=10, leading=13, alignment=TA_CENTER),
        "box": ParagraphStyle("box", fontName=FONT_BOLD, fontSize=9, leading=10, alignment=TA_CENTER),
    }


def _field_row(label, value, width_ratio=None):
    """1 dòng 'Nhãn: ..........giá trị..........'"""
    st = _styles()["normal"]
    val = value if value else BLANK * 4
    return Paragraph(f"<b>{label}:</b> {val}", st)


def _checkbox(label, checked=False):
    box = "☒" if checked else "☐"
    return f"{box} {label}"


def _boxes_row_table(label_text, val_str, max_len=12, label_width=90 * mm):
    val_str = str(val_str or "").strip()
    digits = list(val_str[:max_len]) + [""] * max(0, max_len - len(val_str))
    st_norm = _styles()["normal"]
    st_box = _styles()["box"]

    cells = [Paragraph(f"<b>{label_text}:</b>", st_norm)] + [Paragraph(d, st_box) for d in digits]
    col_widths = [label_width] + [4.2 * mm] * max_len

    t = Table([cells], colWidths=col_widths, rowHeights=[5.2 * mm])
    t.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('ALIGN', (0,0), (0,0), 'LEFT'),
        ('ALIGN', (1,0), (-1,0), 'CENTER'),
        ('BOX', (1,0), (-1,0), 0.5, colors.black),
        ('INNERGRID', (1,0), (-1,0), 0.5, colors.black),
        ('LEFTPADDING', (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 0),
        ('TOPPADDING', (0,0), (-1,-1), 0),
        ('BOTTOMPADDING', (0,0), (-1,-1), 0),
    ]))
    return t


def build_patient_form(rec: dict, group: dict, hospital_name: str = "BỆNH VIỆN HỒNG ĐỨC II") -> bytes:
    """rec: dict các trường bản ghi bệnh nhân (đã lấy từ Record model).
    group: dict thông tin đoàn khám (tên đoàn, địa điểm, đối tượng, hình thức chi trả)."""
    styles = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            topMargin=10 * mm, bottomMargin=10 * mm,
                            leftMargin=15 * mm, rightMargin=15 * mm)
    story = []

    # 1. Dòng đầu tiên trên cùng (Font size 12.5pt, in đậm, căn giữa như Mau02.pdf)
    m2_style = ParagraphStyle("m2_top", fontName=FONT_BOLD, fontSize=12.5, leading=16, alignment=TA_CENTER)
    story.append(Paragraph("Mẫu 2. MẪU GIẤY KHÁM SỨC KHỎE VÀ KHÁM SỨC KHỎE ĐỊNH KỲ DÙNG CHO TRẺ TỪ ĐỦ 06 TUỔI ĐẾN 18 TUỔI", m2_style))
    story.append(Paragraph("-----", styles["center"]))
    story.append(Spacer(1, 2 * mm))

    # 2. Bảng Header 2 cột (Trái: Logo + Tên BV + Số / Phải: Quốc hiệu)
    logo_path = os.path.join(_ROOT_DIR, "static", "images", "logo_hongduc2.png")
    left_cell = []
    if os.path.exists(logo_path):
        left_cell.append(Image(logo_path, width=48 * mm, height=16 * mm))
    left_cell.append(Paragraph(f"<b>{hospital_name.upper()}</b>", styles["small"]))
    left_cell.append(Spacer(1, 1 * mm))
    left_cell.append(Paragraph(f"Số: {BLANK}/GKSK-{BLANK}", styles["normal"]))

    right_cell = [
        Paragraph("<b>CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM</b>", styles["title"]),
        Paragraph("<b>Độc lập - Tự do - Hạnh phúc</b>", styles["sub"]),
        Paragraph("_________________________", styles["sub"]),
    ]

    header_tbl = Table([[left_cell, right_cell]], colWidths=[65 * mm, 115 * mm])
    header_tbl.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('ALIGN', (0,0), (0,0), 'LEFT'),
        ('ALIGN', (1,0), (1,0), 'CENTER'),
        ('LEFTPADDING', (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 0),
    ]))
    story.append(header_tbl)
    story.append(Spacer(1, 3 * mm))

    # 3. Tiêu đề MẪU 02 nằm ở giữa phía dưới "Số: ...."
    story.append(Paragraph("<b>MẪU 02 - GIẤY KHÁM SỨC KHỎE VÀ KHÁM SỨC KHỎE ĐỊNH KỲ DÙNG CHO TRẺ TỪ ĐỦ 06 TUỔI ĐẾN DƯỚI 18 TUỔI</b>", styles["title"]))
    story.append(Paragraph("(Ban hành kèm theo Thông tư số 25/2026/TT-BYT ngày 30 tháng 6 năm 2026 của Bộ Y tế)", styles["sub"]))
    story.append(Spacer(1, 3 * mm))

    # ---------- I. Thông tin hành chính (Đầy đủ 15 mục theo Mẫu 02) ----------
    story.append(Paragraph("THÔNG TIN HÀNH CHÍNH", styles["h2"]))
    story.append(_field_row("1. Họ và tên (viết chữ in hoa)", (rec.get("ho_ten") or "").upper()))
    gioi = rec.get("gioi_tinh") or ""
    story.append(Paragraph(
        f"2. Giới tính: {_checkbox('Nam', gioi=='Nam')} &nbsp;&nbsp;&nbsp;&nbsp; {_checkbox('Nữ', gioi=='Nữ')}",
        styles["normal"]))
    story.append(_field_row("3. Ngày tháng năm sinh", rec.get("ngay_sinh")))
    story.append(_field_row("4. Dân tộc", rec.get("dan_toc")))
    story.append(_field_row("5. Nhóm máu (nếu có)", ""))

    # 6. CCCD - BẢNG ĐỘC LẬP 1 (12 ô)
    story.append(_boxes_row_table("6. Số CCCD/Mã số định danh/Hộ chiếu", rec.get("cccd"), 12, 90 * mm))
    story.append(Spacer(1, 2.5 * mm))

    # 7. BHYT - BẢNG ĐỘC LẬP 2 (15 ô)
    story.append(_boxes_row_table("7. Số thẻ BHYT", rec.get("ma_bhyt"), 15, 90 * mm))
    story.append(Spacer(1, 2.5 * mm))

    dia_chi = ", ".join(x for x in [rec.get("so_nha"), rec.get("khu_pho"), rec.get("phuong"), rec.get("tinh")] if x)
    story.append(_field_row("8. Nơi ở hiện tại", dia_chi))
    story.append(_field_row("Xã/Phường", rec.get("phuong")))

    # 9, 10, 11. Thông tin trường học
    ten_truong = group.get("ten_doan") or ""
    story.append(Paragraph(
        f"9. Trẻ có đi học: &nbsp;&nbsp; {_checkbox('Có', True)} &nbsp;&nbsp;&nbsp;&nbsp; {_checkbox('Không (chuyển qua câu 12)', False)}",
        styles["normal"]))
    story.append(_field_row("10. Tên Trường (nếu có)", ten_truong))
    story.append(_field_row("11. Địa chỉ trường", group.get("dia_diem") or ""))
    story.append(_field_row("Xã/Phường", ""))

    # 12, 13, 14, 15. Thông tin người giám hộ & liên hệ
    story.append(_field_row("12. Họ và tên mẹ hoặc người giám hộ (đối với trẻ ≤16 tuổi)", ""))
    story.append(_field_row("CCCD của mẹ hoặc người giám hộ", ""))
    story.append(Paragraph(
        f"13. Mối quan hệ với trẻ: {_checkbox('Cha')} &nbsp; {_checkbox('Mẹ')} &nbsp; {_checkbox('Ông/bà')} &nbsp; {_checkbox('Anh/chị')} &nbsp; {_checkbox('Họ hàng')} &nbsp; {_checkbox('Khác')}",
        styles["normal"]))
    story.append(_field_row("14. Điện thoại di động", rec.get("so_dien_thoai")))
    story.append(_field_row("15. Lý do khám sức khỏe", "Khám sức khỏe học sinh / định kỳ"))
    story.append(Spacer(1, 2 * mm))

    story.append(Paragraph("THÔNG TIN ĐỐI TƯỢNG - CHI TRẢ", styles["h3"]))
    story.append(_field_row("Đối tượng khám", group.get("doi_tuong_kham")))
    story.append(_field_row("Hình thức chi trả", group.get("hinh_thuc_chi_tra")))
    story.append(_field_row("Địa điểm khám", group.get("dia_diem")))
    story.append(_field_row("Đoàn khám", group.get("ten_doan")))
    story.append(Spacer(1, 3 * mm))
    story.append(HRFlowable(width="100%", thickness=0.7, color=colors.grey))
    story.append(Spacer(1, 2 * mm))

    # ---------- II. Tiền sử bệnh tật (để trống cho người khám/khách khai) ----------
    story.append(Paragraph("II. TIỀN SỬ BỆNH TẬT", styles["h2"]))
    story.append(Paragraph(
        f"1. Tiền sử gia đình (bệnh bẩm sinh / truyền nhiễm): {_checkbox('Không')} {_checkbox('Có, ghi rõ')} "
        f"{BLANK * 6}", styles["normal"]))
    story.append(Paragraph(
        f"2. Tiền sử bản thân (bệnh mãn tính, dị ứng, đang điều trị...): {BLANK * 8}", styles["normal"]))
    story.append(Spacer(1, 2 * mm))

    story.append(Paragraph(
        "Tôi xin cam đoan những điều khai trên đây hoàn toàn đúng với sự thật theo hiểu biết của tôi.",
        styles["small"]))
    story.append(Spacer(1, 4 * mm))

    sig_data = [[Paragraph("", styles["normal"]),
                 Paragraph(f"…………, ngày ….. tháng ….. năm ……<br/><b>Người đề nghị khám sức khỏe</b><br/>"
                          f"(Ký, ghi rõ họ tên)", styles["center"])]]
    t = Table(sig_data, colWidths=[90 * mm, 78 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(t)
    story.append(Spacer(1, 6 * mm))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.black))

    # ---------- III. Khám thể lực ----------
    story.append(Paragraph("III. KHÁM THỂ LỰC", styles["h2"]))
    body_rows = [
        [f"- Chiều cao: {BLANK} cm", f"Mạch: {BLANK} lần/phút"],
        [f"- Cân nặng: {BLANK} kg", f"Huyết áp: {BLANK} mmHg"],
        [f"- Chỉ số BMI: {BLANK}", f"Nhịp thở: {BLANK} lần/phút"],
    ]
    bt = Table(body_rows, colWidths=[84 * mm, 84 * mm])
    bt.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), FONT), ("FONTSIZE", (0, 0), (-1, -1), 10),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story.append(bt)
    story.append(Paragraph(
        "Phân loại thể lực: " + "  ".join(_checkbox(f"Loại {r}") for r in ["I", "II", "III", "IV", "V"]),
        styles["normal"]))
    story.append(Spacer(1, 3 * mm))

    # ---------- IV. Khám lâm sàng theo chuyên khoa ----------
    story.append(Paragraph("IV. KHÁM LÂM SÀNG", styles["h2"]))
    specialties = [
        "1. Nội khoa (Tuần hoàn - Hô hấp - Tiêu hóa - Thận-Tiết niệu - Nội tiết)",
        "2. Ngoại khoa - Cơ xương khớp",
        "3. Thần kinh",
        "4. Tâm thần",
        "5. Mắt",
        "6. Tai - Mũi - Họng",
        "7. Răng - Hàm - Mặt",
        "8. Da liễu",
        "9. Sản phụ khoa (nếu có)",
    ]
    for sp in specialties:
        story.append(Paragraph(f"<b>{sp}</b>", styles["normal"]))
        story.append(Paragraph(
            f"{_checkbox('Chưa phát hiện bất thường')} &nbsp;&nbsp; Chẩn đoán: {BLANK*4} &nbsp;&nbsp; "
            + "Phân loại: " + "  ".join(_checkbox(f"{r}") for r in ["I", "II", "III", "IV", "V"]),
            styles["small"]))
        story.append(Spacer(1, 1.2 * mm))

    story.append(Spacer(1, 2 * mm))

    # ---------- V. Cận lâm sàng ----------
    story.append(Paragraph("V. CẬN LÂM SÀNG", styles["h2"]))
    story.append(Paragraph(f"Xét nghiệm máu: {BLANK*8}", styles["normal"]))
    story.append(Paragraph(f"Xét nghiệm nước tiểu: {BLANK*8}", styles["normal"]))
    story.append(Paragraph(f"Chẩn đoán hình ảnh (X-quang, siêu âm...): {BLANK*8}", styles["normal"]))
    story.append(Paragraph(f"Khác: {BLANK*8}", styles["normal"]))
    story.append(Spacer(1, 3 * mm))

    # ---------- VI. Kết luận ----------
    story.append(Paragraph("VI. KẾT LUẬN", styles["h2"]))
    story.append(Paragraph(f"1. Tình trạng sức khỏe: {_checkbox('Chưa phát hiện bất thường')}", styles["normal"]))
    story.append(Paragraph(f"Chẩn đoán: {BLANK*6}", styles["normal"]))
    story.append(Paragraph(
        "2. Phân loại sức khỏe: " + "  ".join(_checkbox(f"Loại {r}") for r in ["I", "II", "III", "IV", "V"]),
        styles["normal"]))
    story.append(Paragraph(f"3. Đề nghị: {BLANK*8}", styles["normal"]))
    story.append(Spacer(1, 8 * mm))

    sig2 = [[Paragraph("", styles["normal"]),
            Paragraph(f"Ngày ….. tháng ….. năm ……<br/><b>NGƯỜI KẾT LUẬN</b><br/>"
                     f"(Ký, ghi rõ họ tên và đóng dấu)", styles["center"])]]
    t2 = Table(sig2, colWidths=[90 * mm, 78 * mm])
    t2.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(t2)

    def _footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont(FONT, 8)
        canvas.setFillColor(colors.grey)
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm,
                               f"Mã BN: {rec.get('id','')}  ·  In lúc {datetime.now().strftime('%d/%m/%Y %H:%M')}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


def merge_pdfs(pdf_bytes_list: list) -> bytes:
    from pypdf import PdfWriter
    writer = PdfWriter()
    for b in pdf_bytes_list:
        writer.append(io.BytesIO(b))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
