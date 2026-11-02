import io
import re
import zipfile
from pathlib import Path, PurePosixPath

import img2pdf
from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter


class PDFProcessor:
    def normalize_image(self, payload: bytes, quality: str = "original") -> bytes:
        if quality in {"high", "compact"}:
            image = Image.open(io.BytesIO(payload))
            image = ImageOps.exif_transpose(image)
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=85 if quality == "high" else 70, optimize=True)
            return buffer.getvalue()

        image = Image.open(io.BytesIO(payload))
        image.verify()
        return payload

    def create_chapter_pdf(self, images: list[bytes], output_path: str, quality: str = "original") -> None:
        normalized = [self.normalize_image(image, quality) for image in images]
        layout = img2pdf.get_layout_fun(None)
        with open(output_path, "wb") as output:
            output.write(img2pdf.convert(normalized, layout_fun=layout))

    def create_zip(self, chapters: list[tuple[str, list[bytes]]], output_path: str, quality: str = "original", folder_name: str = "TankoPress") -> None:
        folder_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", folder_name).strip() or "TankoPress"
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for index, (chapter, images) in enumerate(chapters, start=1):
                chapter_path = output_path + f".{index}.pdf"
                self.create_chapter_pdf(images, chapter_path, quality)
                number_match = re.match(r"^Chapter\s+(\d+(?:\.\d+)?)", chapter, re.I)
                name = f"Chapter {number_match.group(1)}" if number_match else re.sub(r"[^A-Za-z0-9._ -]+", "_", chapter).strip() or f"Chapter {index}"
                archive.write(chapter_path, f"{folder_name}/{PurePosixPath(name).name}.pdf")
            for index in range(1, len(chapters) + 1):
                Path(output_path + f".{index}.pdf").unlink(missing_ok=True)

    def merge_chapters(self, chapters: list[tuple[str, list[bytes]]], output_path: str, quality: str = "original") -> None:
        writer = PdfWriter()
        outlines = []
        for chapter_name, images in chapters:
            chapter_path = output_path + f".{len(outlines)}.tmp.pdf"
            self.create_chapter_pdf(images, chapter_path, quality)
            reader = PdfReader(chapter_path)
            start = len(writer.pages)
            writer.append(reader)
            outlines.append((chapter_name, start))
        for title, page_number in outlines:
            writer.add_outline_item(title, page_number)
        with open(output_path, "wb") as output:
            writer.write(output)
        for index in range(len(chapters)):
            Path(output_path + f".{index}.tmp.pdf").unlink(missing_ok=True)
