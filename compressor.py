import subprocess
import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def compress_pdf(input_path, power=2):
    """
    Komprimiert ein PDF mit Ghostscript.
    Optimiert für Zeitungen (viele Bilder, Mischung aus Text/Grafik).
    """
    input_path = Path(input_path)
    if not input_path.exists():
        logger.error(f"Komprimierung fehlgeschlagen: Datei nicht gefunden {input_path}")
        return False

    output_path = input_path.with_name(f"{input_path.stem}_temp.pdf")

    # Ghostscript Befehl - Aggressiv auf RGB und JPEG optimiert
    # Zeitungen sind oft CMYK und unkomprimiert -> RGB + JPEG spart massiv Platz
    cmd = [
        'ghostscript',
        '-sDEVICE=pdfwrite',
        '-dCompatibilityLevel=1.4',
        '-dPDFSETTINGS=/ebook',  # Basis: 150 DPI
        '-dNOPAUSE', '-dQUIET', '-dBATCH',
        '-dDetectDuplicateImages=true',

        # Erzwinge RGB (Spart Platz gegenüber CMYK)
        '-sColorConversionStrategy=RGB',
        '-dProcessColorModel=/DeviceRGB',

        # Erzwinge JPEG Komprimierung für Bilder (statt lossless Flate)
        '-dAutoFilterColorImages=false',
        '-dColorImageFilter=/DCTEncode',
        '-dAutoFilterGrayImages=false',
        '-dGrayImageFilter=/DCTEncode',

        # Downsampling strikt anwenden
        '-dColorImageDownsampleType=/Bicubic',
        '-dColorImageResolution=144',
        '-dGrayImageDownsampleType=/Bicubic',
        '-dGrayImageResolution=144',

        f'-sOutputFile={str(output_path)}',
        str(input_path)
    ]

    try:
        logger.info(f"Starte optimierte Komprimierung für {input_path.name}...")
        subprocess.run(cmd, check=True)

        old_size = input_path.stat().st_size
        new_size = output_path.stat().st_size

        if new_size < old_size:
            ratio = (1 - (new_size / old_size)) * 100
            logger.info(
                f"Komprimierung erfolgreich: {old_size / 1024 / 1024:.2f}MB -> {new_size / 1024 / 1024:.2f}MB (-{ratio:.1f}%)")

            # Original überschreiben
            os.remove(input_path)
            os.rename(output_path, input_path)
            return True
        else:
            logger.info(f"Komprimierung hat Größe nicht verringert ({new_size} >= {old_size}). Behalte Original.")
            os.remove(output_path)
            return False

    except subprocess.CalledProcessError as e:
        logger.error(f"Ghostscript Fehler: {e}")
        if output_path.exists():
            os.remove(output_path)
        return False
    except Exception as e:
        logger.error(f"Allgemeiner Fehler bei Komprimierung: {e}")
        if output_path.exists():
            os.remove(output_path)
        return False