# StickerApp

Watches a folder of photo-booth pictures and prints + die-cuts each one as a sticker on
Liene PixCut S1 printers, with a local dashboard, a Reprint button, paper tracking and a
printer pool (round-robin, skipping busy or sleeping printers).

Built for photo booths running Windows with dslrBooth, but it works with anything that drops
1200 x 2100 images into a folder.

> **Not affiliated with Liene.** This is an independent project. It talks to the printer over
> USB using a protocol worked out by observing a printer you own. Use it at your own risk:
> a real print uses a sticker sheet, so try the simulator first.

## Quick start

Try it with fake printers (no hardware needed):

    python -m venv .venv
    .venv/bin/python -m pip install pillow numpy opencv-python-headless    # Windows: .venv\Scripts\python.exe
    .venv/bin/python app/main.py --simulate 2 --sim-seconds 5 --open

Then open **Settings** in the dashboard (http://localhost:8080/) and choose a folder to watch.

For real printers on Windows, install everything in `requirements.txt`, close the Liene Photo
app (it claims the same USB interfaces) and run `run_stickerapp.bat`.

Full details, printer states and paper tracking are in [app/README.md](app/README.md).

## Feedback

Issues and pull requests are welcome.

## License

MIT, see [LICENSE](LICENSE).
