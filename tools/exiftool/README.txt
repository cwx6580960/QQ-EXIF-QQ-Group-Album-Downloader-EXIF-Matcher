Optional ExifTool integration
=============================

Place the official Windows 64-bit ExifTool distribution in this directory:

  tools\exiftool\exiftool.exe
  tools\exiftool\exiftool_files\...

If the downloaded executable is named exiftool(-k).exe, it is also detected.
Keep the exiftool_files directory beside the executable as required by the
official installation instructions.

Official site: https://exiftool.org/

The archiver only invokes ExifTool on files under 01_library. Files under
00_raw are never passed to ExifTool.
