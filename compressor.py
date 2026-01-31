import subprocess
import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def run_ghostscript(input_path, output_path, quality_mode='balanced'):
    """
    Führt den Ghostscript Befehl aus.
    quality_mode: 'balanced' (144 dpi) oder 'aggressive' (96 dpi)
    """

    # Einstellungen für die Modi
    if quality_mode == 'aggressive':
        pdf_settings = '/screen'
        dpi = '96'
    else:
        pdf_settings = '/ebook'
        dpi = '144'

    cmd = [
        'ghostscript',
        '-sDEVICE=pdfwrite',
        '-dCompatibilityLevel=1.4',
        f'-dPDFSETTINGS={pdf_settings}',
        '-dNOPAUSE', '-dQUIET', '-dBATCH',
        '-dDetectDuplicateImages=true',
        '-dCompressFonts=true',
        '-dSubsetFonts=true',

        # RGB erzwingen (spart Platz bei CMYK Zeitungen)
        '-sColorConversionStrategy=RGB',
        '-dProcessColorModel=/DeviceRGB',

        # Bilder: JPEG Komprimierung erzwingen
        '-dAutoFilterColorImages=false',
        '-dColorImageFilter=/DCTEncode',
        '-dAutoFilterGrayImages=false',
        '-dGrayImageFilter=/DCTEncode',

        # Downsampling
        '-dColorImageDownsampleType=/Bicubic',
        f'-dColorImageResolution={dpi}',
        '-dGrayImageDownsampleType=/Bicubic',
        f'-dGrayImageResolution={dpi}',

        f'-sOutputFile={str(output_path)}',
        str(input_path)
    ]

    subprocess.run(cmd, check=True)


def compress_pdf(input_path):
    """
    Versucht ein PDF intelligent zu komprimieren.
    Strategie: Erst moderat (144dpi), wenn das nichts bringt -> aggressiv (96dpi).
    """
    input_path = Path(input_path)
    if not input_path.exists():
        logger.error(f"Datei nicht gefunden: {input_path}")
        return False

    temp_path = input_path.with_name(f"{input_path.stem}_temp.pdf")
    original_size = input_path.stat().st_size

    try:
        # --- VERSUCH 1: Balanced (144 DPI) ---
        logger.info(f"Starte Komprimierung (Balanced/144dpi) für {input_path.name}...")
        run_ghostscript(input_path, temp_path, 'balanced')

        new_size = temp_path.stat().st_size
        ratio = (1 - (new_size / original_size)) * 100

        # Wenn weniger als 10% Ersparnis (oder Vergrößerung), versuche es härter
        if new_size >= original_size or ratio < 10:
            logger.info(f"Balanced brachte zu wenig ({ratio:.1f}%) oder Vergrößerung. Starte Aggressive (96dpi)...")

            # Temp Datei löschen für neuen Versuch
            if temp_path.exists():
                os.remove(temp_path)

            # --- VERSUCH 2: Aggressive (96 DPI) ---
            run_ghostscript(input_path, temp_path, 'aggressive')
            new_size = temp_path.stat().st_size
            ratio = (1 - (new_size / original_size)) * 100

        # Finale Auswertung
        if new_size < original_size:
            logger.info(
                f"Optimierung erfolgreich: {original_size / 1024 / 1024:.2f}MB -> {new_size / 1024 / 1024:.2f}MB (-{ratio:.1f}%)")
            os.remove(input_path)
            os.rename(temp_path, input_path)
            return True
        else:
            logger.info(
                f"Keine Optimierung möglich (Datei wächst auf {new_size / 1024 / 1024:.2f}MB). Behalte Original.")
            if temp_path.exists():
                os.remove(temp_path)
            return False

    except subprocess.CalledProcessError as e:
        logger.error(f"Ghostscript Fehler: {e}")
        if temp_path.exists():
            os.remove(temp_path)
        return False
    except Exception as e:
        logger.error(f"Fehler bei Komprimierung: {e}")
        if temp_path.exists():
            os.remove(temp_path)
        return False