"""Copy the mockup's canvas engine, verbatim, into ui/themes/emiya/tests/mockup_engine_ref.js.

The parity test (ui/themes/emiya/tests/port_parity.mjs) runs this reference next to the port with the same
random numbers and compares every canvas call. The copy keeps the test self-contained once the mockup's
temporary folder is gone. Run once, from the app folder:

    venv\\Scripts\\python.exe tools\\extract_mockup_engine.py <path to Main.dc.html> [<output file>]
"""
import hashlib
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / 'ui' / 'themes' / 'emiya' / 'tests' / 'mockup_engine_ref.js'
FIRST, LAST = 'fxInit(W, H) {', '  // ---------- render'   # fxInit ... fxDraw, the engine without the React glue


def main(source: Path, out: Path = OUT) -> None:
    """Write the slice fxInit ... fxDraw of `source` to `out` as an ES module class MockupEngine."""
    text = source.read_text(encoding='utf-8')
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == FIRST)
    end = next(i for i, line in enumerate(lines) if i > start and line.startswith(LAST))
    body = '\n'.join(lines[start:end]).rstrip()
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        '// VERBATIM copy of the Emiya Voice mockup canvas engine (fxInit ... fxDraw), for the parity test only.\n'
        f'// Source: {source.name}, lines {start + 1}-{end}, sha256 {digest}.\n'
        '// Not shipped: this file holds the mockup\'s colour literals and fixed coordinates on purpose.\n'
        'export class MockupEngine {\n'
        f'{body}\n'
        '}\n',
        encoding='utf-8')
    print(f'wrote {out} from lines {start + 1}-{end} of {source}')


if __name__ == '__main__':
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) == 3 else OUT)
