import subprocess
import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def compress_pdf(input_path, power=2):
    """
    Komprimiert ein PDF mit Ghostscript.
    power:
        0: default
        1: pre-press (300 dpi, hohe Qualität)
        2: ebook (150 dpi, gute Qualität, mittlere Größe) -> UNSER STANDARD
        3: screen (72 dpi, kleine Größe, Bilder pixelig)
    """
    input_path = Path(input_path)
    if not input_path.exists():
        logger.error(f"Komprimierung fehlgeschlagen: Datei nicht gefunden {input_path}")
        return False

    # Temp Output Datei
    output_path = input_path.with_name(f"{input_path.stem}_temp.pdf")

    # Ghostscript Qualitäts-Einstellungen
    quality = {
        0: '/default',
        1: '/prepress',
        2: '/ebook',
        3: '/screen'
    }

    gs_setting = quality.get(power, '/ebook')

    # Der Ghostscript Befehl
    # -dPDFSETTINGS=... setzt die Qualität
    # -dCompatibilityLevel=1.4 sorgt für Kompatibilität
    cmd = [
        'ghostscript',
        '-sDEVICE=pdfwrite',
        f'-dCompatibilityLevel=1.4',
        f'-dPDFSETTINGS={gs_setting}',
        '-dNOPAUSE',
        '-dQUIET',
        '-dBATCH',
        f'-sOutputFile={str(output_path)}',
        str(input_path)
    ]

    try:
        logger.info(f"Starte Komprimierung für {input_path.name} (Modus: {gs_setting})...")
        subprocess.run(cmd, check=True)

        # Checken ob es was gebracht hat
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
        return False