"""Command-line entry point."""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from . import __version__
from .adapters import AdapterDetector, AdapterError
from .config import ConfigError, load_config
from .exporter import recover_journals
from .gps import list_serial_ports
from .ui import print_banner
from .wlan import WlanError


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="spejari",
        description="Spejari for Windows - multi-adapter Wi-Fi war-driving scout (2.4 / 5 GHz) with GPS and "
        "WiGLE CSV export.",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--cli", action="store_true", help="terminal dashboard")
    mode.add_argument("--gui", action="store_true", help="desktop window (CustomTkinter)")
    p.add_argument("-c", "--config", help="path to a TOML config file (default: config\\spejari.toml)")
    p.add_argument("-i", "--interface", action="append", default=[], metavar="ADAPTER",
                   help="use only this adapter: tag, GUID or part of its name (repeatable)")
    p.add_argument("-x", "--exclude", action="append", default=[], metavar="ADAPTER",
                   help="never use this adapter, e.g. the one carrying your internet link (repeatable)")
    p.add_argument("-n", "--max-adapters", type=int, metavar="N", help="adapter limit (1-10)")
    p.add_argument("-b", "--band", choices=("both", "2.4", "5"), help="record only this band")
    p.add_argument("--cycle", type=float, metavar="SECONDS", help="scan cycle per adapter (default 4)")
    p.add_argument("--gps", choices=("auto", "serial", "gpsd", "none"), help="GPS source")
    p.add_argument("--gps-device", metavar="COMx", help="GPS COM port, e.g. COM5 (implies --gps serial)")
    p.add_argument("--gpsd", metavar="HOST:PORT", help="gpsd address (default 127.0.0.1:2947)")
    p.add_argument("-o", "--output-dir", metavar="DIR", help="where CSV exports are written")
    p.add_argument("--auto-start", action="store_true", help="start scanning immediately")
    p.add_argument("--simulate", nargs="?", const=3, type=int, metavar="N",
                   help="no hardware needed: N simulated adapters (default 3) and a simulated GPS drive")
    p.add_argument("--list-adapters", action="store_true", help="show detected Wi-Fi adapters, then exit")
    p.add_argument("--list-gps", action="store_true", help="show COM ports that could be a GPS receiver, then exit")
    p.add_argument("--recover", action="store_true", help="convert journals of crashed sessions into CSV files")
    p.add_argument("-V", "--version", action="store_true", help="print version and exit")
    return p


def apply_overrides(cfg, args) -> None:
    if args.interface:
        cfg.scan.include_interfaces = list(args.interface)
    if args.exclude:
        cfg.scan.exclude_interfaces = list(cfg.scan.exclude_interfaces) + list(args.exclude)
    if args.max_adapters:
        cfg.scan.max_adapters = args.max_adapters
    if args.band:
        cfg.scan.band_2g = args.band in ("both", "2.4")
        cfg.scan.band_5g = args.band in ("both", "5")
    if args.cycle:
        cfg.scan.scan_cycle_s = args.cycle
    if args.gps:
        cfg.gps.source = args.gps
    if args.gps_device:
        cfg.gps.serial_port = args.gps_device
        if not args.gps:
            cfg.gps.source = "serial"
    if args.gpsd:
        host, _, port = args.gpsd.partition(":")
        cfg.gps.gpsd_host = host or cfg.gps.gpsd_host
        if port.isdigit():
            cfg.gps.gpsd_port = int(port)
    if args.output_dir:
        cfg.log.output_dir = args.output_dir
    if args.simulate is not None:
        cfg.simulate_adapters = max(1, min(12, args.simulate))
        cfg.gps.source = "sim"
        cfg.scan.scan_cycle_s = args.cycle or 2.0  # snappy demo
    cfg.normalise()


def make_backend(cfg):
    """The real Windows WLAN API, or the simulator for --simulate."""
    if cfg.simulate_adapters:
        from .sim import SimBackend

        return SimBackend(cfg.simulate_adapters)
    from .wlan import WlanClient

    return WlanClient()


def list_adapters(cfg, console: Console) -> int:
    print_banner(console)
    try:
        backend = make_backend(cfg)
        adapters = AdapterDetector(cfg.scan, backend).detect()
    except (WlanError, AdapterError) as exc:
        console.print(f"[red]{exc}[/]")
        return 1
    if not adapters:
        console.print("[yellow]No Wi-Fi adapters found. Is Wi-Fi switched on and the WLAN AutoConfig service running?[/]")
        return 0
    t = Table(title="Wi-Fi adapters", header_style="bold")
    for col in ("Tag", "Adapter", "State", "GUID"):
        t.add_column(col)
    for a in adapters:
        t.add_row(a.tag, a.description, a.state_text, a.guid)
    console.print(t)
    console.print("Use the tag, GUID or part of the name with  -i / --interface  and  -x / --exclude.")
    return 0


def list_gps(console: Console) -> int:
    ports = list_serial_ports()
    if not ports:
        console.print("No COM ports found. Plug in the GPS receiver and check Device Manager > Ports (COM & LPT).")
        return 0
    t = Table(title="COM ports", header_style="bold")
    for col in ("Port", "USB id", "Description", "u-blox?"):
        t.add_column(col)
    for p in ports:
        usb = f"{int(p['vid']):04X}:{int(p['pid']):04X}" if p["vid"] else "-"
        t.add_row(str(p["port"]), usb, str(p["description"]), "yes" if p["ublox"] else "")
    console.print(t)
    return 0


def recover(cfg, console: Console) -> int:
    paths = recover_journals(cfg.output_path)
    if not paths:
        console.print("No unfinished session journals found.")
    for p in paths:
        console.print(f"[green]Recovered:[/] {p}")
    return 0


def choose_mode(args, cfg, console: Console) -> str:
    """CLI or GUI: explicit flag > config default > interactive chooser."""
    if args.gui:
        return "gui"
    if args.cli:
        return "cli"
    if cfg.ui.default_mode in ("cli", "gui"):
        return cfg.ui.default_mode
    has_display = os.name == "nt" or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if not (sys.stdin and sys.stdin.isatty()):  # e.g. double-clicked / pythonw: the TUI cannot work
        return "gui" if has_display else "cli"
    console.print("[bold]Choose an interface:[/]")
    console.print("  [cyan]1[/]  CLI   terminal dashboard")
    console.print("  [cyan]2[/]  GUI   desktop window (CustomTkinter)")
    default = "2" if has_display else "1"
    answer = Prompt.ask("Select", choices=["1", "2", "cli", "gui"], default=default, console=console)
    return "gui" if answer in ("2", "gui") else "cli"


def _use_utf8() -> None:
    """Rich draws box characters and runes: make sure the console encoding can print them."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def main(argv: Optional[List[str]] = None) -> int:
    _use_utf8()
    args = build_parser().parse_args(argv)
    console = Console()
    if args.version:
        print(f"Spejari for Windows {__version__}")
        return 0
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/]")
        return 2
    apply_overrides(cfg, args)

    if args.list_adapters:
        return list_adapters(cfg, console)
    if args.list_gps:
        return list_gps(console)
    if args.recover:
        return recover(cfg, console)

    print_banner(console)
    for warning in cfg.warnings:
        console.print(f"[yellow]config: {warning}[/]")
    try:
        backend = make_backend(cfg)
    except WlanError as exc:
        console.print(f"[red]{exc}[/]")
        return 1

    mode = choose_mode(args, cfg, console)
    from .app import App  # imported late so --help / --list work quickly

    app = App(cfg, backend, auto_start=args.auto_start)
    if mode == "gui":
        try:
            from .gui import run_gui
        except ImportError as exc:
            console.print(
                f"[red]The GUI needs CustomTkinter ({exc}).[/]\n"
                "Install it with:  pip install customtkinter   (or run setup.bat), "
                "or use the terminal version with --cli."
            )
            return 1
        return run_gui(app)
    return app.run()
