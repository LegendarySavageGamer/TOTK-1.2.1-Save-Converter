import os
import re
import json
import shutil
import socket
import subprocess
import threading
import random
import time
import queue
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from totk_converter_core import (
    convert_ktml_to_progress, convert_progress_to_ktml, parse_ktml,
    serialize_ktml, validate_ktml_types, normalize_ktml_types,
)


def resource_path(name):
    import sys
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    return base / name


class ServerManagerFrame(ttk.Frame):
    """Temporary room launcher and player-save manager for Kirbymimi's server."""

    PORT_MIN = 10000
    PORT_MAX = 12000
    DEFAULT_UPLOAD_BPS = 10_000_000
    RECOMMENDED_UPLOAD_BPS = 1_000_000
    MAX_JAVA_INT = 2_147_483_647

    def __init__(self, parent, muted='#9BAAA4', accent='#D8B85B'):
        super().__init__(parent, padding=18)
        self.muted = muted
        self.accent = accent
        self.proc = None
        self.host_engine_var = tk.StringVar(value='Docker Desktop (Recommended)')
        self.container_name = None
        self.docker_image = 'wesleyhellewell/totk_online_multiplayer_server_test:latest'
        self.docker_container_port = 10014
        self._docker_running_cached = False
        self._docker_log_proc = None
        self._docker_status_check_in_progress = False
        self._last_docker_status_check = 0.0
        # Console output is buffered so a noisy Java/Docker process cannot flood
        # Tkinter with thousands of cross-thread callbacks and make the GUI sluggish.
        self._log_queue = queue.Queue(maxsize=4000)
        self._dropped_log_lines = 0
        self.verbose_console_var = tk.BooleanVar(value=False)
        self._hidden_save_sync_lines = 0
        self._last_hidden_sync_notice = 0.0
        self.root_workspace = Path.home() / 'SacredRealmsOfTheKingdom'
        self.rooms_dir = self.root_workspace / 'Rooms'
        self.workspace = None

        self.active_room_name = tk.StringVar(value='No active room')
        self.port_var = tk.StringVar(value='—')
        self.primary_ip_var = tk.StringVar(value='Detecting…')
        self.primary_ip_label_var = tk.StringVar(value='')
        self._ip_option_map = {}
        self.status = tk.StringVar(value='Offline')
        self.connection_state = tk.StringVar(value='No room open')
        self.duration_var = tk.StringVar(value='2 hours')
        self.countdown_var = tk.StringVar(value='00:00:00')

        # Room options are copied into the room configuration before Java starts.
        self.pvp_enabled_var = tk.BooleanVar(value=True)
        self.upload_speed_var = tk.StringVar(value=str(self.RECOMMENDED_UPLOAD_BPS))
        self.upload_speed_label_var = tk.StringVar(value='1.00 MB/s • Safe / Recommended')

        self.deadline = None
        self.paused_remaining_seconds = None
        self.realm_paused = False
        self._expired_stop = False
        self._known_user_mtimes = {}
        self._online_names = set()
        self._game_ready_names = set()
        self._pending_connect_name = None
        self._pending_leave_name = None
        self._active_client_name = None
        self._client_connected_at = None
        self._last_client_event_at = None
        self._player_meta = {}
        self._sav_template_path = None

        self._build()
        self.after(250, self._poll)

    # ------------------------------------------------------------------ UI
    def _build(self):
        hero = ttk.Frame(self, style='Panel.TFrame')
        hero.pack(fill='x', pady=(0, 14))
        ttk.Label(hero, text='Zonai Realms Hosting', style='SectionTitle.TLabel').pack(anchor='w')
        ttk.Label(
            hero,
            text='Open a temporary multiplayer room, control room rules, monitor players, and manage individual server saves from one place.',
            style='Muted.TLabel', wraplength=1000,
        ).pack(anchor='w', pady=(4, 0))

        state = ttk.Frame(self, style='Card.TFrame', padding=14)
        state.pack(fill='x', pady=(0, 12))
        ttk.Label(state, text='ROOM STATUS', style='Eyebrow.TLabel').grid(row=0, column=0, sticky='w')
        ttk.Label(state, textvariable=self.status, style='CardGold.TLabel').grid(row=1, column=0, sticky='w', pady=(2, 0))
        ttk.Label(state, text='CONNECTION', style='Eyebrow.TLabel').grid(row=0, column=1, sticky='w', padx=(36, 0))
        ttk.Label(state, textvariable=self.connection_state, style='Muted.TLabel').grid(row=1, column=1, sticky='w', padx=(36, 0), pady=(2, 0))
        ttk.Label(state, text='TIME REMAINING', style='Eyebrow.TLabel').grid(row=0, column=2, sticky='w', padx=(36, 0))
        ttk.Label(state, textvariable=self.countdown_var, style='CardGold.TLabel').grid(row=1, column=2, sticky='w', padx=(36, 0), pady=(2, 0))
        state.columnconfigure(3, weight=1)

        setup = ttk.LabelFrame(self, text='Room Setup', padding=14)
        setup.pack(fill='x', pady=(0, 12))

        ttk.Label(setup, text='Hosting engine').grid(row=0, column=0, sticky='w', pady=5)
        self.engine_combo = ttk.Combobox(
            setup, textvariable=self.host_engine_var, state='readonly', width=29,
            values=['Docker Desktop (Recommended)', 'Native Java (Legacy)'],
        )
        self.engine_combo.grid(row=0, column=1, sticky='w', padx=(10, 24), pady=5)

        ttk.Label(setup, text='Room duration').grid(row=1, column=0, sticky='w', pady=5)
        self.duration_combo = ttk.Combobox(
            setup, textvariable=self.duration_var, state='readonly', width=14,
            values=[f'{i} hour' if i == 1 else f'{i} hours' for i in range(1, 9)],
        )
        self.duration_combo.grid(row=1, column=1, sticky='w', padx=(10, 24), pady=5)

        ttk.Label(setup, text='PvP / puppet collision').grid(row=1, column=2, sticky='w', pady=5)
        self.pvp_check = ttk.Checkbutton(setup, text='Enabled', variable=self.pvp_enabled_var)
        self.pvp_check.grid(row=1, column=3, sticky='w', padx=(10, 24), pady=5)

        ttk.Label(setup, text='Max resource upload').grid(row=1, column=4, sticky='w', pady=5)
        self.upload_entry = ttk.Entry(setup, textvariable=self.upload_speed_var, width=14)
        self.upload_entry.grid(row=1, column=5, sticky='w', padx=(10, 6), pady=5)
        self.upload_entry.bind('<FocusOut>', lambda _e: self._update_upload_label())
        ttk.Label(setup, text='bytes/sec', style='Muted.TLabel').grid(row=1, column=6, sticky='w', pady=5)
        ttk.Label(setup, textvariable=self.upload_speed_label_var, style='Gold.TLabel').grid(row=2, column=5, columnspan=2, sticky='w', padx=(10, 0))

        ttk.Label(setup, text='Port').grid(row=2, column=0, sticky='w', pady=5)
        ttk.Label(setup, textvariable=self.port_var, style='Gold.TLabel').grid(row=2, column=1, sticky='w', padx=(10, 24), pady=5)

        ttk.Label(setup, text='Primary IP Address').grid(row=2, column=2, sticky='w', pady=5)
        self.primary_ip_combo = ttk.Combobox(setup, textvariable=self.primary_ip_var, width=31, state='normal')
        self.primary_ip_combo.grid(row=2, column=3, sticky='w', padx=(10, 6), pady=5)
        ttk.Button(setup, text='↻ Refresh IP', command=self.refresh_primary_ip).grid(row=2, column=4, sticky='w', padx=(0, 6), pady=5)
        ttk.Button(setup, text='VPN Diagnostics', command=self.vpn_diagnostics).grid(row=2, column=5, sticky='w', padx=(0, 6), pady=5)
        ttk.Button(setup, text='Radmin Direct Mode', command=self.use_radmin_direct_mode).grid(row=2, column=6, sticky='w', padx=(0, 6), pady=5)
        ttk.Button(setup, text='Allow Port in Firewall', command=self.allow_realm_port_firewall).grid(row=2, column=7, sticky='w', padx=(0, 6), pady=5)
        ttk.Label(setup, textvariable=self.primary_ip_label_var, style='Muted.TLabel').grid(row=3, column=2, columnspan=6, sticky='w', padx=(10,0), pady=(0,4))

        ttk.Label(setup, text='Active realm').grid(row=3, column=0, sticky='w', pady=5)
        ttk.Label(setup, textvariable=self.active_room_name, style='Muted.TLabel').grid(row=3, column=1, sticky='w', padx=(10, 24), pady=5)
        setup.columnconfigure(8, weight=1)
        self.after(50, self.refresh_primary_ip)

        presets = ttk.Frame(setup, style='Panel.TFrame')
        presets.grid(row=4, column=0, columnspan=9, sticky='ew', pady=(8, 0))
        ttk.Label(presets, text='Transfer presets:', style='Muted.TLabel').pack(side='left')
        for label, value in [('1 MB/s  SAFE', 1_000_000), ('2.5 MB/s', 2_500_000), ('5 MB/s', 5_000_000), ('10 MB/s  ORIGINAL', 10_000_000)]:
            ttk.Button(presets, text=label, command=lambda v=value: self._set_upload_speed(v)).pack(side='left', padx=(6, 0))
        ttk.Button(presets, text='Recommended Settings', command=self._reset_recommended).pack(side='right')
        ttk.Label(
            setup,
            text="Tip: 1 MB/s is the tested safe setting. VPNs are supported: connect your VPN first, click Refresh IP, then choose the VPN/virtual-adapter address if it is listed. The IP field is editable for providers that give you a specific reachable VPN IP.",
            style='Muted.TLabel', wraplength=1050,
        ).grid(row=5, column=0, columnspan=9, sticky='w', pady=(8, 0))

        actions = ttk.Frame(self, style='Panel.TFrame')
        actions.pack(fill='x', pady=(0, 12))
        self.start_btn = ttk.Button(actions, text='✦  Open New Realm', command=self.start, style='Accent.TButton')
        self.start_btn.pack(side='left')
        self.pause_btn = ttk.Button(actions, text='⏸  Pause / Stop Realm', command=self.pause_realm)
        self.pause_btn.pack(side='left', padx=(6, 0))
        self.resume_btn = ttk.Button(actions, text='▶  Resume Realm', command=self.resume_realm)
        self.resume_btn.pack(side='left', padx=(6, 0))
        self.stop_btn = ttk.Button(actions, text='■  Close Realm', command=self.stop)
        self.stop_btn.pack(side='left', padx=(6, 0))
        ttk.Button(actions, text='Copy Connection', command=self.connection_info).pack(side='left', padx=6)
        ttk.Button(actions, text='Run Diagnostics', command=self.run_diagnostics).pack(side='left', padx=6)
        ttk.Button(actions, text='Black Screen Help', command=self.black_screen_help).pack(side='left', padx=6)
        ttk.Button(actions, text='Open Realm Folder', command=self.open_folder).pack(side='right')

        body = ttk.Panedwindow(self, orient='horizontal')
        body.pack(fill='both', expand=True)

        players_card = ttk.LabelFrame(body, text='Players & Individual Saves', padding=8)
        console_card = ttk.LabelFrame(body, text='Live Realm Console', padding=8)
        body.add(players_card, weight=3)
        body.add(console_card, weight=4)

        player_toolbar = ttk.Frame(players_card, style='Panel.TFrame')
        player_toolbar.pack(fill='x', pady=(0, 8))
        ttk.Button(player_toolbar, text='↻  Refresh Players', command=self.refresh_players).pack(side='left')
        ttk.Label(
            player_toolbar,
            text='Pause / Stop the realm before replacing a save or using Actor Sync Recovery.',
            style='Muted.TLabel',
        ).pack(side='left', padx=(12, 0))

        # Zonai-style player rows: status dot, name, last seen, format selector,
        # Download and Upload / Replace controls all stay beside each player.
        self.players_canvas = tk.Canvas(players_card, bg='#0D1B18', highlightthickness=0, height=250)
        player_scroll = ttk.Scrollbar(players_card, orient='vertical', command=self.players_canvas.yview)
        self.players_inner = ttk.Frame(self.players_canvas, style='Panel.TFrame')
        self._players_window = self.players_canvas.create_window((0, 0), window=self.players_inner, anchor='nw')
        self.players_canvas.configure(yscrollcommand=player_scroll.set)
        self.players_inner.bind('<Configure>', lambda _e: self.players_canvas.configure(scrollregion=self.players_canvas.bbox('all')))
        self.players_canvas.bind('<Configure>', lambda e: self.players_canvas.itemconfigure(self._players_window, width=e.width))
        self.players_canvas.pack(side='left', fill='both', expand=True)
        player_scroll.pack(side='right', fill='y')
        self._player_row_widgets = []

        console_toolbar=ttk.Frame(console_card,style='Panel.TFrame')
        console_toolbar.pack(fill='x',pady=(0,6))
        ttk.Checkbutton(console_toolbar,text='Detailed save-sync log',variable=self.verbose_console_var).pack(side='left')
        ttk.Label(console_toolbar,text='Off = hides repetitive “Applying save request” lines for a smoother UI.',style='Muted.TLabel').pack(side='left',padx=(10,0))
        ttk.Button(console_toolbar,text='Clear Console',command=lambda:self.log.delete('1.0','end')).pack(side='right')
        log_wrap=ttk.Frame(console_card,style='Panel.TFrame'); log_wrap.pack(fill='both',expand=True)
        self.log = tk.Text(
            log_wrap, bg='#090D0C', fg='#DDE8E2', insertbackground='#F4D57A',
            selectbackground='#31584B', font=('Consolas', 9), wrap='word',
            relief='flat', padx=10, pady=10,
        )
        ys = ttk.Scrollbar(log_wrap, orient='vertical', command=self.log.yview)
        self.log.configure(yscrollcommand=ys.set)
        self.log.pack(side='left', fill='both', expand=True)
        ys.pack(side='right', fill='y')

        self.pause_btn.state(['disabled'])
        self.resume_btn.state(['disabled'])
        self.stop_btn.state(['disabled'])
        self._update_upload_label()

    # ------------------------------------------------------------ settings
    def _duration_hours(self):
        try:
            return max(1, min(8, int(self.duration_var.get().split()[0])))
        except Exception:
            return 2

    def _set_upload_speed(self, value):
        self.upload_speed_var.set(str(int(value)))
        self._update_upload_label()

    def _reset_recommended(self):
        """Return room options to the tested stable client baseline."""
        self.pvp_enabled_var.set(True)
        self._set_upload_speed(self.RECOMMENDED_UPLOAD_BPS)

    def _reset_server_defaults(self):
        """Return room options to Kirbymimi's original 10 MB/s server default."""
        self.pvp_enabled_var.set(True)
        self._set_upload_speed(self.DEFAULT_UPLOAD_BPS)

    def _validated_upload_speed(self):
        raw = self.upload_speed_var.get().replace(',', '').strip()
        try:
            value = int(raw)
        except ValueError:
            raise ValueError('Max resource upload must be a whole number in bytes per second.')
        if value < 1 or value > self.MAX_JAVA_INT:
            raise ValueError(f'Upload speed must be between 1 and {self.MAX_JAVA_INT:,} bytes/sec.')
        return value

    def _update_upload_label(self):
        try:
            value = self._validated_upload_speed()
            mb = value / 1_000_000
            if value <= self.RECOMMENDED_UPLOAD_BPS:
                suffix = ' • Safe / Recommended'
            elif value >= 5_000_000:
                suffix = ' • May cause black-screen loading'
            else:
                suffix = ' • Moderate'
            self.upload_speed_label_var.set(f'{mb:.2f} MB/s{suffix}')
        except Exception:
            self.upload_speed_label_var.set('Invalid value')

    def _set_setup_enabled(self, enabled):
        if enabled:
            self.engine_combo.configure(state='readonly')
            self.duration_combo.configure(state='readonly')
            self.pvp_check.state(['!disabled'])
            self.upload_entry.state(['!disabled'])
        else:
            self.engine_combo.configure(state='disabled')
            self.duration_combo.configure(state='disabled')
            self.pvp_check.state(['disabled'])
            self.upload_entry.state(['disabled'])

    # ----------------------------------------------------------- utilities
    def _using_docker(self):
        return self.host_engine_var.get().startswith('Docker Desktop')

    def _docker_cli(self):
        return shutil.which('docker')

    def _docker_ready(self, show_error=True):
        docker = self._docker_cli()
        if not docker:
            if show_error:
                messagebox.showerror(
                    'Docker Desktop required',
                    'Docker Desktop / docker.exe was not found. Start Docker Desktop and make sure the Docker CLI is available, or choose Native Java (Legacy).',
                    parent=self,
                )
            return False
        try:
            result = subprocess.run(
                [docker, 'version', '--format', '{{.Server.Version}}'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                timeout=8, creationflags=(subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0),
            )
            if result.returncode != 0 or not result.stdout.strip():
                raise RuntimeError(result.stderr.strip() or 'Docker engine is not responding.')
            return True
        except Exception as exc:
            if show_error:
                messagebox.showerror(
                    'Docker Desktop is not ready',
                    f'Start Docker Desktop and wait until the engine says it is running.\n\n{exc}',
                    parent=self,
                )
            return False

    def _ensure_docker_image(self):
        docker = self._docker_cli()
        inspect = subprocess.run(
            [docker, 'image', 'inspect', self.docker_image],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=8, creationflags=(subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0),
        )
        if inspect.returncode == 0:
            return True
        self.status.set('Preparing Docker Java image…')
        self.connection_state.set('First Docker start may take a few minutes')
        self.update_idletasks()
        pull = subprocess.run(
            [docker, 'pull', self.docker_image],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            timeout=300, creationflags=(subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0),
        )
        if pull.stdout:
            for line in pull.stdout.splitlines():
                self._append('[Docker] ' + line)
        if pull.returncode != 0:
            raise RuntimeError('Docker could not download the Java 21 runtime image. Check Docker Desktop and your internet connection.')
        return True

    def _docker_container_exists(self):
        if not self.container_name or not self._docker_cli():
            return False
        r = subprocess.run(
            [self._docker_cli(), 'container', 'inspect', self.container_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0),
        )
        return r.returncode == 0

    def _docker_container_running(self):
        if not self._docker_container_exists():
            return False
        r = subprocess.run(
            [self._docker_cli(), 'inspect', '-f', '{{.State.Running}}', self.container_name],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0),
        )
        return r.returncode == 0 and r.stdout.strip().lower() == 'true'

    def _create_docker_container(self, port):
        docker = self._docker_cli()
        if not self.workspace:
            raise RuntimeError('Realm workspace has not been created.')
        self._ensure_kirbymimi_runtime_layout()
        safe = re.sub(r'[^a-z0-9_.-]+', '-', self.active_room_name.get().lower()).strip('-')
        self.container_name = ('sacred-' + safe)[:60]
        # Remove only an old stopped container with this exact generated name.
        if self._docker_container_exists():
            subprocess.run([docker, 'rm', '-f', self.container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=(subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0))
        # v7.30 Docker image hotfix: the verified Wesley _test image contains
        # the multiplayer server and its own entrypoint. Persist realm data at
        # /app/run and map the selected host port to container port 10014.
        volume = f'{self.workspace.resolve()}:/app/run'
        cmd = [
            docker, 'create', '--name', self.container_name,
            '--init',
            '--interactive',
            '--stop-timeout', '15',
            '--restart', 'no',
            '-p', f'0.0.0.0:{port}:{self.docker_container_port}',
            '-v', volume,
            self.docker_image,
        ]
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           creationflags=(subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0))
        if r.returncode != 0:
            raise RuntimeError('Docker could not create the realm container:\n\n' + r.stdout.strip())
        self._save_runtime_settings()

    def _save_runtime_settings(self):
        if not self.workspace:
            return
        path = self.workspace / 'SacredRealmSettings.json'
        try:
            data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            data['hosting_engine'] = 'docker' if self._using_docker() else 'native'
            data['docker_container'] = self.container_name
            path.write_text(json.dumps(data, indent=2), encoding='utf-8')
        except Exception:
            pass

    def _launch_docker_attach(self):
        """
        Start the Docker container detached and follow its logs separately.

        Kirbymimi's CommandServer continuously reads System.in. If stdin is
        closed, its readLine() reaches EOF and the server loops on an empty
        command, producing an endless "Unknown command name :" flood.
        """
        docker = self._docker_cli()
        if not docker:
            raise RuntimeError('Docker CLI is unavailable.')

        result = subprocess.run(
            [docker, 'start', self.container_name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            timeout=30,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0),
        )
        if result.returncode != 0:
            raise RuntimeError(
                'Docker could not start the realm container:\n\n' +
                (result.stdout.strip() or 'Unknown Docker error.')
            )

        self._docker_running_cached = True
        self._last_docker_status_check = time.monotonic()

        self._docker_log_proc = subprocess.Popen(
            [docker, 'logs', '-f', '--since', '0s', self.container_name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0),
        )
        self.proc = self._docker_log_proc
        threading.Thread(
            target=self._reader,
            args=(self._docker_log_proc,),
            daemon=True,
        ).start()

    def _refresh_docker_state_async(self):
        """Check Docker state off the Tk main thread."""
        if not self.container_name or self._docker_status_check_in_progress:
            return
        self._docker_status_check_in_progress = True

        def worker():
            try:
                running = self._docker_container_running()
            except Exception:
                running = False

            def apply():
                self._docker_running_cached = running
                self._docker_status_check_in_progress = False
                self._last_docker_status_check = time.monotonic()

            self.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    def _stop_docker_container(self):
        if not self.container_name or not self._docker_container_exists():
            self._docker_running_cached = False
            return

        docker = self._docker_cli()
        self._append(f'Stopping Docker container {self.container_name}…')

        result = subprocess.run(
            [docker, 'stop', '-t', '15', self.container_name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            timeout=30,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0),
        )
        if result.stdout.strip():
            self._append('[Docker] ' + result.stdout.strip())

        if result.returncode != 0 and self._docker_container_running():
            subprocess.run(
                [docker, 'kill', self.container_name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0),
            )

        self._docker_running_cached = False

        log_proc = self._docker_log_proc
        if log_proc and log_proc.poll() is None:
            try:
                log_proc.terminate()
                log_proc.wait(timeout=3)
            except Exception:
                try:
                    log_proc.kill()
                except Exception:
                    pass

        self._docker_log_proc = None
        self.proc = None

    def _remove_docker_container(self):
        if self.container_name and self._docker_container_exists():
            subprocess.run([self._docker_cli(), 'rm', '-f', self.container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=(subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0))
        self.container_name = None

    def _server_running(self):
        if self._using_docker() and self.container_name:
            return bool(self._docker_running_cached)
        return bool(self.proc and self.proc.poll() is None)

    def _append(self, text):
        """Queue console output without touching Tk widgets from worker threads."""
        try:
            self._log_queue.put_nowait(str(text).rstrip())
        except queue.Full:
            self._dropped_log_lines += 1

    def _flush_log_queue(self):
        """Flush a bounded batch of console lines on Tk's main thread."""
        if not hasattr(self, 'log'):
            return
        batch = []
        for _ in range(400):
            try:
                batch.append(self._log_queue.get_nowait())
            except queue.Empty:
                break

        if self._dropped_log_lines:
            batch.insert(0, f'[console] {self._dropped_log_lines:,} excessive log lines were dropped to keep the UI responsive.')
            self._dropped_log_lines = 0

        if not batch:
            return

        # Collapse immediately repeated messages inside the batch. This protects
        # the interface if a server component ever starts spamming one error.
        collapsed = []
        last = None
        repeat = 0
        for line in batch:
            if line == last:
                repeat += 1
                continue
            if last is not None:
                collapsed.append(last if repeat == 1 else f'{last}  [repeated {repeat}×]')
            last = line
            repeat = 1
        if last is not None:
            collapsed.append(last if repeat == 1 else f'{last}  [repeated {repeat}×]')

        self.log.insert('end', '\n'.join(collapsed) + '\n')

        # Keep the console bounded so many hours of hosting do not make Tk Text
        # progressively slower.
        try:
            total_lines = int(self.log.index('end-1c').split('.')[0])
            if total_lines > 3000:
                self.log.delete('1.0', f'{total_lines - 2600}.0')
        except Exception:
            pass
        self.log.see('end')

    @staticmethod
    def _port_free(port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('0.0.0.0', port))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    def _choose_port(self):
        candidates = list(range(self.PORT_MIN, self.PORT_MAX + 1))
        random.SystemRandom().shuffle(candidates)
        for port in candidates:
            if self._port_free(port):
                return port
        raise RuntimeError(f'No free ports were found between {self.PORT_MIN} and {self.PORT_MAX}.')

    def _copy_tree(self, source_root, destination_root):
        for src in source_root.rglob('*'):
            rel = src.relative_to(source_root)
            dst = destination_root / rel
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

    @staticmethod
    def _set_resource_upload_limit(creator_path, upload_bps):
        text = creator_path.read_text(encoding='utf-8', errors='strict')
        # ResourceServer has a real int field named serverMaxUploadPerSecond.
        pattern = re.compile(
            r'(\{\s*"targetClass"\s*:\s*"ResourceServer"\s*"isSyncedWithParent"\s*:\s*false)(\s*\})',
            re.S,
        )
        replacement = (
            r'\1\n\t\t\t"properties": {\n'
            f'\t\t\t\t"serverMaxUploadPerSecond": {int(upload_bps)}\n'
            r'\t\t\t}\2'
        )
        new_text, count = pattern.subn(replacement, text, count=1)
        if count != 1:
            # If a future server template already has properties, update the existing value.
            new_text, count = re.subn(
                r'("serverMaxUploadPerSecond"\s*:\s*)\d+',
                rf'\g<1>{int(upload_bps)}', text, count=1,
            )
        if count != 1:
            raise ValueError('Could not configure ResourceServer upload speed in serverCreator.ktml.')
        creator_path.write_text(new_text, encoding='utf-8')

    @staticmethod
    def _set_player_pvp(actor_creators_path, enabled):
        """Toggle PlayerPuppet collision synchronization only, leaving normal actor collision sync intact."""
        text = actor_creators_path.read_text(encoding='utf-8', errors='strict')
        marker = '"PlayerPuppet": {'
        pos = text.find(marker)
        if pos < 0:
            raise ValueError('PlayerPuppet configuration was not found in actorCreators.ktml.')

        prefix = text[:pos]
        tail = text[pos:]
        collision_block = re.compile(
            r'\n\s*\{\s*"targetClass"\s*:\s*"ActorCollisionSync"\s*\}\s*',
            re.S,
        )
        has_collision = bool(collision_block.search(tail))

        if enabled:
            # The bundled template already contains the component; fail loudly if a future template does not.
            if not has_collision:
                raise ValueError('PvP was requested, but PlayerPuppet ActorCollisionSync is missing from the bundled template.')
            return

        new_tail, count = collision_block.subn('\n', tail, count=1)
        if count != 1:
            raise ValueError('Could not disable PlayerPuppet collision sync.')
        actor_creators_path.write_text(prefix + new_tail, encoding='utf-8')

    def _create_room_workspace(self, port, pvp_enabled, upload_bps):
        bundle = resource_path('server_bundle')
        jar_src = bundle / 'server.jar'
        runtime_src = bundle / 'runtime'
        if not jar_src.exists() or not runtime_src.exists():
            raise FileNotFoundError('The bundled server.jar or runtime resources could not be found.')

        self.rooms_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        room_name = f'Realm_{stamp}_{port}'
        workspace = self.rooms_dir / room_name
        workspace.mkdir(parents=True, exist_ok=False)
        self._link_or_copy_file(jar_src, workspace / 'server.jar')
        self._copy_tree(runtime_src, workspace)
        # Empty folders can disappear from PyInstaller one-file data bundles.
        # Recreate the folder Kirbymimi's SaveServer requires before startup.
        (workspace / 'User' / 'SaveServer' / 'users').mkdir(parents=True, exist_ok=True)

        creator = workspace / 'Resources' / 'TOTKServer' / 'serverCreator.ktml'
        if not creator.exists():
            raise FileNotFoundError('serverCreator.ktml is missing from the room template.')
        text = creator.read_text(encoding='utf-8', errors='strict')
        text2, count = re.subn(r'("port"\s*:\s*)\d+', rf'\g<1>{port}', text, count=1)
        if count != 1:
            raise ValueError('Could not find the port property in serverCreator.ktml.')
        creator.write_text(text2, encoding='utf-8')

        # Keep the original ResourceServer creator byte-for-byte unchanged when
        # the user chooses Kirbymimi's original 10,000,000 bytes/sec built-in default.
        # This gives us the cleanest compatibility baseline for black-screen tests.
        if int(upload_bps) != self.DEFAULT_UPLOAD_BPS:
            self._set_resource_upload_limit(creator, upload_bps)

        actor_creators = workspace / 'Resources' / 'SyncedActorServer' / 'actorCreators.ktml'
        if not actor_creators.exists():
            raise FileNotFoundError('actorCreators.ktml is missing from the room template.')
        self._set_player_pvp(actor_creators, pvp_enabled)

        settings = {
            'room': room_name,
            'created_at': datetime.now().isoformat(timespec='seconds'),
            'port': port,
            'duration_hours': self._duration_hours(),
            'pvp_enabled': bool(pvp_enabled),
            'serverMaxUploadPerSecond': int(upload_bps),
        }
        (workspace / 'SacredRealmSettings.json').write_text(json.dumps(settings, indent=2), encoding='utf-8')
        self.workspace = workspace
        self._ensure_kirbymimi_runtime_layout()
        return workspace, room_name

    def _sanitize_kirbymimi_users_directory(self):
        """Keep User/SaveServer/users limited to actual player save files.

        Kirbymimi's SaveServer enumerates every child of the ``users`` resource
        and attempts to decode each child as a save.  A subdirectory therefore
        crashes startup.  Older Sacred Realms builds incorrectly created a
        ``Backups`` directory inside ``users``; migrate that directory out of the
        server-scanned path before every start/resume.
        """
        if not self.workspace:
            return

        users = self.workspace / 'User' / 'SaveServer' / 'users'
        users.mkdir(parents=True, exist_ok=True)

        legacy = users / 'Backups'
        if legacy.exists() and legacy.is_dir():
            destination = self.workspace / 'Backups' / 'PlayerSaves' / 'MigratedFromUsers'
            destination.mkdir(parents=True, exist_ok=True)
            for child in list(legacy.iterdir()):
                target = destination / child.name
                if target.exists():
                    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
                    target = destination / f'{child.stem}_{stamp}{child.suffix}'
                shutil.move(str(child), str(target))
            try:
                legacy.rmdir()
            except OSError:
                pass
            self._append('[repair] Moved legacy player backups out of User/SaveServer/users so Kirbymimi\'s SaveServer will not try to decode the Backups folder.')

        # No subdirectories belong in Kirbymimi's player-save resource folder.
        # Do not silently move unknown folders; stop with a useful explanation.
        unexpected_dirs = [child.name for child in users.iterdir() if child.is_dir()]
        if unexpected_dirs:
            raise RuntimeError(
                'Kirbymimi\'s User/SaveServer/users folder may contain player save files only. '
                'The following subfolder(s) would crash SaveServer startup: '
                + ', '.join(unexpected_dirs)
                + '\n\nMove those folders somewhere outside User/SaveServer/users, then try again.'
            )

    def _ensure_kirbymimi_runtime_layout(self):
        """Repair/validate the filesystem layout Kirbymimi's JAR expects.

        SaveServer.onInit() immediately asks User/SaveServer for a child resource
        named "users". PyInstaller one-file builds do not reliably preserve empty
        directories, so that folder must be created explicitly before Java starts.
        """
        if not self.workspace:
            raise RuntimeError('Realm workspace has not been created.')

        # These empty directories are meaningful to Kirbymimi's ResourceLoader.
        required_dirs = [
            self.workspace / 'User' / 'SaveServer' / 'users',
            self.workspace / 'User' / 'BanServer',
        ]
        for directory in required_dirs:
            directory.mkdir(parents=True, exist_ok=True)

        # Sacred Realms backups must never live inside Kirbymimi's users resource.
        self._sanitize_kirbymimi_users_directory()

        required_files = [
            self.workspace / 'server.jar',
            self.workspace / 'User' / 'SaveServer' / 'save.ktml',
            self.workspace / 'Resources' / 'SaveServer' / 'defaultClientSave.ktml',
            self.workspace / 'Resources' / 'SaveServer' / 'name2hash.ktml',
            self.workspace / 'Resources' / 'SaveServer' / 'bitKeyMap.ktml',
            self.workspace / 'Resources' / 'TOTKServer' / 'serverCreator.ktml',
            self.workspace / 'Resources' / 'TOTKServer' / 'actorList.ktml',
            self.workspace / 'Resources' / 'SyncedActorServer' / 'actorCreators.ktml',
            self.workspace / 'Romfs' / 'Sequence' / 'Main.module.ainb',
        ]
        missing = [str(path.relative_to(self.workspace)) for path in required_files if not path.exists()]
        if missing:
            raise FileNotFoundError(
                'Kirbymimi server runtime is incomplete. Missing:\n  - ' + '\n  - '.join(missing)
            )

        # Verify the important writable save locations before Docker/Java starts.
        for path in [self.workspace / 'User' / 'SaveServer', self.workspace / 'User' / 'SaveServer' / 'users']:
            probe = path / '.sacred_realms_write_test'
            try:
                probe.write_text('ok', encoding='utf-8')
                probe.unlink(missing_ok=True)
            except Exception as exc:
                raise PermissionError(f'Save folder is not writable: {path}\n\n{exc}') from exc

    @staticmethod
    def _link_or_copy_file(source, destination):
        """Use a fast NTFS hard-link when possible; fall back to a normal copy."""
        try:
            os.link(source, destination)
        except Exception:
            shutil.copy2(source, destination)

    def creator_file(self):
        return (self.workspace / 'Resources' / 'TOTKServer' / 'serverCreator.ktml') if self.workspace else None

    def default_client_save(self):
        return (self.workspace / 'Resources' / 'SaveServer' / 'defaultClientSave.ktml') if self.workspace else None

    # --------------------------------------------------------------- start
    def start(self):
        if self._server_running():
            messagebox.showinfo('Realm already open', 'Pause or close the current realm before opening another one.', parent=self)
            return
        if self.realm_paused and self.workspace:
            messagebox.showinfo('Realm paused', 'This realm is paused. Use Resume Realm to continue it, or Close Realm before opening a new one.', parent=self)
            return

        if self._using_docker():
            if not self._docker_ready():
                return
            try:
                self._ensure_docker_image()
            except Exception as exc:
                messagebox.showerror('Docker setup failed', str(exc), parent=self)
                return
            java = None
        else:
            java = shutil.which('java')
            if not java:
                messagebox.showerror('Java required', 'Java was not found. Install 64-bit Java 17 or newer, then reopen the app.', parent=self)
                return

        try:
            upload_bps = self._validated_upload_speed()
            port = self._choose_port()
            workspace, room_name = self._create_room_workspace(
                port, self.pvp_enabled_var.get(), upload_bps,
            )
        except Exception as exc:
            messagebox.showerror('Could not create realm', str(exc), parent=self)
            return

        self.workspace = workspace
        self.port_var.set(str(port))
        self.active_room_name.set(room_name)
        hours = self._duration_hours()
        self.deadline = datetime.now() + timedelta(hours=hours)
        self.paused_remaining_seconds = None
        self.realm_paused = False
        self._expired_stop = False
        self._online_names.clear(); self._game_ready_names.clear()
        self._active_client_name = None
        self._client_connected_at = None
        self._last_client_event_at = None
        self._known_user_mtimes = self._scan_user_mtimes()
        self._load_player_meta()

        try:
            if self._using_docker():
                self._create_docker_container(port)
                self._launch_docker_attach()
            else:
                self.proc = subprocess.Popen(
                    [java, '-jar', 'server.jar'], cwd=self.workspace,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                    creationflags=(subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0),
                )
                threading.Thread(target=self._reader, daemon=True).start()
                self._save_runtime_settings()
            self.status.set(f'Opening • port {port}')
            self.connection_state.set('Waiting for player')
            self.start_btn.state(['disabled'])
            self.pause_btn.state(['!disabled'])
            self.resume_btn.state(['disabled'])
            self.stop_btn.state(['!disabled'])
            self._set_setup_enabled(False)
            self._append('=== SACRED REALM OPENING ===')
            self._append(f'Realm: {room_name}')
            self._append(f'Port: {port}')
            self._append(f'Duration: {hours} hour(s)')
            self._append(f'PvP / puppet collision: {"ENABLED" if self.pvp_enabled_var.get() else "DISABLED"}')
            self._append(f'Max resource upload: {upload_bps:,} bytes/sec ({upload_bps/1_000_000:.2f} MB/s)')
            if upload_bps > self.RECOMMENDED_UPLOAD_BPS:
                self._append('NOTE: 1 MB/s is the tested stable setting; higher transfer rates may cause black-screen loading on some clients.')
            if upload_bps == self.DEFAULT_UPLOAD_BPS and self.pvp_enabled_var.get():
                self._append('Compatibility baseline: original PvP + original ResourceServer upload default (only room port changed).')
            self._append(f'Workspace: {workspace}')
            self._append(f'Hosting engine: {"Docker Desktop" if self._using_docker() else "Native Java (Legacy)"}')
            self.refresh_players()
        except Exception as exc:
            self.status.set('Offline')
            self.start_btn.state(['!disabled'])
            self.pause_btn.state(['disabled'])
            self.resume_btn.state(['disabled'])
            self.stop_btn.state(['disabled'])
            self._set_setup_enabled(True)
            messagebox.showerror('Server failed to start', str(exc), parent=self)

    def _reader(self, proc=None):
        proc = proc or self.proc
        try:
            if not proc or not proc.stdout:
                return
            for line in proc.stdout:
                clean = line.rstrip()
                low = clean.lower()
                # Save sync can produce hundreds of lines per minute. We still
                # parse every line for state, but keep the normal console quiet
                # unless the user explicitly enables detailed logging.
                if 'applying save request :' in low and not self.verbose_console_var.get():
                    self._hidden_save_sync_lines += 1
                    now_mono=time.monotonic()
                    if now_mono-self._last_hidden_sync_notice>=30.0:
                        hidden=self._hidden_save_sync_lines
                        self._hidden_save_sync_lines=0
                        self._last_hidden_sync_notice=now_mono
                        self._append(f'[save sync] active • {hidden:,} detailed update(s) hidden')
                else:
                    self._append(clean)
                if 'address already in use' in low or 'bindexception' in low:
                    self.after(0, lambda: self.connection_state.set('Port collision detected'))
                    continue

                name = self._extract_player_event(clean, 'connected')
                if name:
                    now = datetime.now()
                    self._online_names.add(name)
                    self._pending_connect_name = name
                    self._active_client_name = name
                    self._client_connected_at = now
                    self._last_client_event_at = now
                    # The server only proves the network handshake here. It does
                    # not emit a reliable "game finished loading" event, so do
                    # not claim that the game is loading or ready.
                    self.after(0, lambda n=name: self.connection_state.set(f'{n} connected • handshake received'))
                    self.after(1000, lambda n=name: self._correlate_save_file(n, leaving=False))
                    self.after(0, self.refresh_players)
                    continue

                # Kirbymimi's actor-sync layer emits this after the connected
                # client has progressed into MainField. This is the first real
                # server-side signal we have that gameplay initialization reached
                # the main world, so use it instead of a guessed timer.
                if 'adding client ' in low and ' to manager mainfield' in low:
                    ready_name = self._pending_connect_name or self._active_client_name
                    if ready_name and ready_name in self._online_names:
                        self._game_ready_names.add(ready_name)
                        self._pending_connect_name = None
                        self.after(0, lambda n=ready_name: self.connection_state.set(f'{n} in game • MainField ready'))
                        self.after(0, self.refresh_players)
                    continue

                name = self._extract_player_event(clean, 'left')
                if name:
                    self._online_names.discard(name)
                    self._game_ready_names.discard(name)
                    self._pending_leave_name = name
                    self._last_client_event_at = datetime.now()
                    if self._active_client_name == name:
                        self._active_client_name = None
                        self._client_connected_at = None
                    self.after(0, lambda n=name: self._set_disconnected_state(n))
                    self.after(800, lambda n=name: self._correlate_save_file(n, leaving=True))
                    self.after(0, self.refresh_players)
                    continue

                if any(word in low for word in ('listening', 'started', 'server started', 'server initialized successfully')):
                    self.after(0, lambda: self.status.set(f'Online • port {self.port_var.get()}'))
        except Exception as exc:
            self._append(f'[console error] {exc}')

    def _set_disconnected_state(self, nickname):
        """Update the top card immediately when the server reports a client left."""
        if self._online_names:
            names = ', '.join(sorted(self._online_names))
            self.connection_state.set(f'{len(self._online_names)} player(s) connected • {names}')
        else:
            self.connection_state.set(f'{nickname} disconnected • waiting for player')

    @staticmethod
    def _extract_player_event(line, event):
        # Kirbymimi's TOTKServer logs "<nickname> connected" and "<nickname> left".
        m = re.search(rf'([^\[\]:]+?)\s+{re.escape(event)}\s*$', line, flags=re.I)
        if not m:
            return None
        name = m.group(1).strip()
        # Avoid interpreting general status text as a nickname.
        if not name or name.lower() in {'server', 'client', 'player'}:
            return None
        return name

    def _wait_for_port_release(self, port, timeout=12.0):
        """Wait for Windows/Java to fully release a room port after shutdown."""
        end = time.time() + timeout
        while time.time() < end:
            if self._port_free(port):
                return True
            time.sleep(0.25)
        return self._port_free(port)

    def _terminate_server_process(self, message='Stopping Java server...'):
        """Stop the active realm engine and wait until its host port is actually free."""
        if self._using_docker() and self.container_name:
            self._append(message)
            try:
                port = int(self.port_var.get())
            except Exception:
                port = None
            self._stop_docker_container()
            if port is not None:
                released = self._wait_for_port_release(port, timeout=12.0)
                self._append(f'Port {port} released successfully.' if released else f'Port {port} is still busy after Docker stopped.')
            return True

        proc = self.proc
        port = None
        try:
            port = int(self.port_var.get())
        except Exception:
            pass

        if proc and proc.poll() is None:
            self._append(message)
            pid = proc.pid
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:
                # On Windows, kill the process tree that belongs to this realm.
                # This prevents a detached Java child from keeping the room port
                # and player save files locked after Pause / Stop Realm.
                if os.name == 'nt':
                    try:
                        subprocess.run(
                            ['taskkill', '/PID', str(pid), '/T', '/F'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=8, check=False,
                        )
                    except Exception:
                        pass
                try:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass

        self.proc = None

        if port is not None:
            released = self._wait_for_port_release(port, timeout=12.0)
            if released:
                self._append(f'Port {port} released successfully.')
            else:
                self._append(f'Port {port} is still busy after shutdown; Resume will automatically move this realm to a free port if needed.')
        return True

    def _set_workspace_port(self, new_port):
        """Change the paused realm to a different free port without rebuilding its saves."""
        creator = self.creator_file()
        if not creator or not creator.exists():
            raise FileNotFoundError('serverCreator.ktml is missing from this realm.')
        text = creator.read_text(encoding='utf-8', errors='strict')
        text2, count = re.subn(r'("port"\s*:\s*)\d+', rf'\g<1>{int(new_port)}', text, count=1)
        if count != 1:
            raise ValueError('Could not update the paused realm port.')
        creator.write_text(text2, encoding='utf-8')
        settings_path = self.workspace / 'SacredRealmSettings.json'
        if settings_path.exists():
            try:
                data = json.loads(settings_path.read_text(encoding='utf-8'))
                data['port'] = int(new_port)
                settings_path.write_text(json.dumps(data, indent=2), encoding='utf-8')
            except Exception:
                pass
        self.port_var.set(str(new_port))

    def pause_realm(self):
        """Stop Java but keep this exact room, port, save data, and countdown for later resume."""
        if not self.workspace or not self._server_running():
            messagebox.showinfo('Nothing to pause', 'There is no running realm to pause.', parent=self)
            return
        if self.deadline:
            self.paused_remaining_seconds = max(0, int((self.deadline - datetime.now()).total_seconds()))
        else:
            self.paused_remaining_seconds = 0
        self._terminate_server_process('Pausing realm safely...')
        self.realm_paused = True
        self.deadline = None
        self.status.set(f'Paused • port {self.port_var.get()} reserved for resume')
        self.connection_state.set('Realm paused • saves can now be managed safely')
        self._online_names.clear(); self._game_ready_names.clear()
        self._active_client_name = None
        self._client_connected_at = None
        self.start_btn.state(['disabled'])
        self.pause_btn.state(['disabled'])
        self.resume_btn.state(['!disabled'])
        self.stop_btn.state(['!disabled'])
        self._set_setup_enabled(False)
        self._append('=== REALM PAUSED ===')
        self._append('Server stopped. Room files, exact port, and countdown are preserved.')
        self._append('You may now download, edit, upload, or replace individual player saves.')
        self.refresh_players()

    def resume_realm(self):
        """Restart a stopped realm without changing its port, workspace, saves, or countdown."""
        if not self.realm_paused or not self.workspace:
            messagebox.showinfo('Nothing to resume', 'There is no stopped realm to resume.', parent=self)
            return
        try:
            port = int(self.port_var.get())
        except Exception:
            messagebox.showerror('Invalid room port', 'The saved room port is invalid.', parent=self)
            return

        # Never silently change ports on Resume. The client/server session expects
        # the same endpoint. Docker Desktop is specifically used here so Stop
        # releases the mapping and Start can restore the exact same mapping.
        if not self._port_free(port):
            messagebox.showerror(
                'Realm port is still busy',
                f'Port {port} is still in use, so Sacred Realms will NOT change it automatically.\n\n'
                'If this is a Docker realm, make sure Docker Desktop has finished stopping the realm. '
                'If another program took the port, close that program and press Resume Realm again.',
                parent=self,
            )
            return

        try:
            self._ensure_kirbymimi_runtime_layout()

            if self._using_docker():
                if not self._docker_ready():
                    return
                if not self._docker_container_exists():
                    self._ensure_docker_image()
                    self._create_docker_container(port)
                self._launch_docker_attach()
            else:
                java = shutil.which('java')
                if not java:
                    messagebox.showerror('Java required', 'Java was not found. Install 64-bit Java 17 or newer.', parent=self)
                    return
                self.proc = subprocess.Popen(
                    [java, '-jar', 'server.jar'], cwd=self.workspace,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                    creationflags=(subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0),
                )
                threading.Thread(target=self._reader, daemon=True).start()

            remaining = max(0, int(self.paused_remaining_seconds or 0))
            self.deadline = datetime.now() + timedelta(seconds=remaining)
            self.realm_paused = False
            self.status.set(f'Resuming • port {port}')
            self.connection_state.set('Waiting for player')
            self._online_names.clear(); self._game_ready_names.clear()
            self._active_client_name = None
            self._client_connected_at = None
            self.start_btn.state(['disabled'])
            self.pause_btn.state(['!disabled'])
            self.resume_btn.state(['disabled'])
            self.stop_btn.state(['!disabled'])
            self._append('=== REALM RESUMING ===')
            self._append(f'Same room, same saves, SAME port: {port}')
            self._append(f'Hosting engine: {"Docker Desktop" if self._using_docker() else "Native Java (Legacy)"}')
            self.refresh_players()
        except Exception as exc:
            self.proc = None
            self.realm_paused = True
            messagebox.showerror('Could not resume realm', str(exc), parent=self)

    def stop(self, expired=False):
        """Permanently end the active room session. Room files remain on disk."""
        self._terminate_server_process('Closing realm...')
        if self._using_docker():
            self._remove_docker_container()
        self.deadline = None
        self.paused_remaining_seconds = None
        self.realm_paused = False
        self.countdown_var.set('00:00:00')
        self.status.set('Expired' if expired else 'Closed')
        self.connection_state.set('Realm closed')
        self.start_btn.state(['!disabled'])
        self.pause_btn.state(['disabled'])
        self.resume_btn.state(['disabled'])
        self.stop_btn.state(['disabled'])
        self._set_setup_enabled(True)
        self._online_names.clear(); self._game_ready_names.clear()
        self._active_client_name = None
        self._client_connected_at = None
        self._append('=== REALM EXPIRED ===' if expired else '=== REALM CLOSED ===')
        self.refresh_players()

    def _poll(self):
        self._flush_log_queue()
        if self.deadline and self._server_running():
            remaining = int((self.deadline - datetime.now()).total_seconds())
            if remaining <= 0:
                self.countdown_var.set('00:00:00')
                if not self._expired_stop:
                    self._expired_stop = True
                    self._append('Time limit reached. Closing the realm automatically...')
                    self.stop(expired=True)
            else:
                h, rem = divmod(remaining, 3600)
                m, s = divmod(rem, 60)
                self.countdown_var.set(f'{h:02d}:{m:02d}:{s:02d}')
                if self.status.get().startswith('Opening'):
                    self.status.set(f'Online • port {self.port_var.get()}')

        # The Java log gives us connect/leave events plus a concrete actor-sync
        # MainField event. Until MainField appears, report the connection as
        # awaiting gameplay initialization instead of guessing readiness.
        if self._active_client_name and self._client_connected_at and self._active_client_name in self._online_names:
            age = int((datetime.now() - self._client_connected_at).total_seconds())
            if self._active_client_name in self._game_ready_names:
                self.connection_state.set(f'{self._active_client_name} in game • MainField ready')
            elif age >= 12:
                self.connection_state.set(f'{self._active_client_name} online • awaiting MainField')

        if self._using_docker() and self.container_name:
            now_mono = time.monotonic()
            if now_mono - self._last_docker_status_check >= 5.0:
                self._refresh_docker_state_async()

        if not self._using_docker():
            if self.proc and self.proc.poll() is not None:
                code = self.proc.returncode
                self.proc = None
                self.deadline = None
                self.countdown_var.set('00:00:00')
                self.status.set(f'Offline • exit {code}')
                self.connection_state.set('Server exited')
                self.start_btn.state(['!disabled'])
                self.pause_btn.state(['disabled'])
                self.resume_btn.state(['disabled'])
                self.stop_btn.state(['disabled'])
                self._set_setup_enabled(True)
                self._online_names.clear(); self._game_ready_names.clear()
                self._active_client_name = None
                self._client_connected_at = None
                self._append(f'=== SERVER EXITED ({code}) ===')
                self.refresh_players()

        self.after(250, self._poll)

    # ---------------------------------------------------------- player list
    def _users_dir(self):
        return (self.workspace / 'User' / 'SaveServer' / 'users') if self.workspace else None

    def _player_meta_file(self):
        return (self.workspace / 'SacredRealmPlayers.json') if self.workspace else None

    def _load_player_meta(self):
        self._player_meta = {}
        path = self._player_meta_file()
        if path and path.exists():
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    self._player_meta = data
            except Exception:
                self._player_meta = {}

    def _save_player_meta(self):
        path = self._player_meta_file()
        if not path:
            return
        try:
            path.write_text(json.dumps(self._player_meta, indent=2), encoding='utf-8')
        except Exception as exc:
            self._append(f'[player metadata warning] {exc}')

    def _scan_user_mtimes(self):
        result = {}
        users = self._users_dir()
        if users and users.exists():
            for p in users.glob('*.ktml'):
                try:
                    result[p.stem] = p.stat().st_mtime
                except OSError:
                    pass
        return result

    def _correlate_save_file(self, nickname, leaving=False):
        """Associate a console nickname with the UID save most recently created/updated."""
        if not self.workspace:
            return
        current = self._scan_user_mtimes()
        changed = []
        for uid, mtime in current.items():
            old = self._known_user_mtimes.get(uid)
            if old is None or mtime > old + 0.0001:
                changed.append((mtime, uid))
        self._known_user_mtimes = current

        if changed:
            changed.sort(reverse=True)
            uid = changed[0][1]
            entry = self._player_meta.setdefault(uid, {})
            entry['name'] = nickname
            entry['last_seen'] = datetime.now().isoformat(timespec='seconds')
            entry['status'] = 'offline' if leaving else 'online'
            self._save_player_meta()
        else:
            # Existing users may not write at connect; try to find a previously learned nickname.
            for uid, entry in self._player_meta.items():
                if entry.get('name') == nickname:
                    entry['last_seen'] = datetime.now().isoformat(timespec='seconds')
                    entry['status'] = 'offline' if leaving else 'online'
                    self._save_player_meta()
                    break
        self.refresh_players()

    def _clear_player_rows(self):
        if not hasattr(self, 'players_inner'):
            return
        for child in self.players_inner.winfo_children():
            child.destroy()
        self._player_row_widgets = []

    def _user_file_for_uid(self, uid):
        users = self._users_dir()
        if not users:
            return None
        p = users / f'{uid}.ktml'
        return p if p.exists() else None

    def _render_player_header(self):
        header = ttk.Frame(self.players_inner, style='Card.TFrame', padding=(10, 7))
        header.pack(fill='x', pady=(0, 4))
        for col, text, width in [(0, 'PLAYER', 20), (1, 'STATUS', 10), (2, 'LAST SEEN', 22), (3, 'SAVE', 9), (4, 'FORMAT', 9), (5, 'ACTIONS', 44)]:
            ttk.Label(header, text=text, style='Eyebrow.TLabel', width=width).grid(row=0, column=col, sticky='w', padx=(0, 8))
        header.columnconfigure(5, weight=1)

    def _add_player_row(self, uid, name, online, last_seen, size_text, mapped=True):
        row = ttk.Frame(self.players_inner, style='Panel.TFrame', padding=(10, 8))
        row.pack(fill='x', pady=2)
        dot = tk.Label(row, text='●', bg='#0D1B18', fg=('#71C79B' if online else '#7E8B86'), font=('Segoe UI', 10, 'bold'))
        dot.grid(row=0, column=0, sticky='w')
        ttk.Label(row, text=name, width=18).grid(row=0, column=1, sticky='w', padx=(4, 8))
        ready = online and name in self._game_ready_names
        status_text = 'In Game' if ready else ('Online' if online else 'Offline')
        ttk.Label(row, text=status_text, width=10, style=('Gold.TLabel' if online else 'Muted.TLabel')).grid(row=0, column=2, sticky='w', padx=(0, 8))
        ttk.Label(row, text=last_seen, width=24, style='Muted.TLabel').grid(row=0, column=3, sticky='w', padx=(0, 8))
        ttk.Label(row, text=size_text, width=8, style='Muted.TLabel').grid(row=0, column=4, sticky='w', padx=(0, 8))
        fmt = tk.StringVar(value='.ktml')
        combo = ttk.Combobox(row, textvariable=fmt, state='readonly', width=7, values=['.ktml', '.sav'])
        combo.grid(row=0, column=5, sticky='w', padx=(0, 6))
        download = ttk.Button(row, text='Download', command=lambda u=uid, n=name, f=fmt: self.download_player_save(u, n, f.get()))
        download.grid(row=0, column=6, sticky='w', padx=(0, 6))
        upload = ttk.Button(row, text='Upload / Replace', command=lambda u=uid, n=name: self.replace_player_save(u, n))
        upload.grid(row=0, column=7, sticky='w', padx=(0, 6))
        recover = ttk.Button(row, text='Actor Sync Recovery Lab', command=lambda u=uid, n=name: self.actor_sync_recovery_tool(u, n))
        recover.grid(row=0, column=8, sticky='w')
        if not mapped:
            combo.configure(state='disabled')
            download.state(['disabled'])
            upload.state(['disabled'])
            recover.state(['disabled'])
        row.columnconfigure(9, weight=1)
        self._player_row_widgets.append((row, fmt))

    def refresh_players(self):
        if not hasattr(self, 'players_inner'):
            return
        self._clear_player_rows()
        self._render_player_header()
        if not self.workspace:
            ttk.Label(self.players_inner, text='Open a realm to see players and individual saves.', style='Muted.TLabel').pack(anchor='w', padx=12, pady=16)
            return
        if not self._player_meta:
            self._load_player_meta()

        users = self._users_dir()
        learned_online = set()
        rendered = 0
        if users and users.exists():
            files = sorted(users.glob('*.ktml'), key=lambda p: p.stat().st_mtime, reverse=True)
            for p in files:
                uid = p.stem
                meta = self._player_meta.get(uid, {})
                name = meta.get('name') or f'Player {uid[:8]}'
                online = name in self._online_names
                if online:
                    learned_online.add(name)
                try:
                    stat = p.stat()
                    file_stamp = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %I:%M:%S %p')
                    size_text = f'{stat.st_size/1024:.0f} KB'
                except OSError:
                    file_stamp, size_text = 'Unknown', '—'
                if online:
                    last_seen = 'In game now' if name in self._game_ready_names else 'Online now'
                else:
                    raw_last = meta.get('last_seen')
                    if raw_last:
                        try:
                            last_seen = datetime.fromisoformat(raw_last).strftime('%Y-%m-%d %I:%M:%S %p')
                        except Exception:
                            last_seen = str(raw_last)
                    else:
                        last_seen = file_stamp
                self._add_player_row(uid, name, online, last_seen, size_text, mapped=True)
                rendered += 1

        for name in sorted(self._online_names - learned_online):
            self._add_player_row('', name, True, 'In game now' if name in self._game_ready_names else 'Online now', 'Mapping…', mapped=False)
            rendered += 1

        if rendered == 0:
            ttk.Label(self.players_inner, text='No player saves found yet. Players will appear here after connecting.', style='Muted.TLabel').pack(anchor='w', padx=12, pady=16)

    def download_player_save(self, uid, player_name, fmt):
        src = self._user_file_for_uid(uid)
        if not src:
            messagebox.showerror('Save unavailable', 'This player save is not mapped yet.', parent=self)
            return
        safe_name = re.sub(r'[^A-Za-z0-9_.-]', '_', player_name)
        if fmt == '.sav':
            self._export_player_sav(src, safe_name)
        else:
            dst = filedialog.asksaveasfilename(
                parent=self, title='Download player KTML', defaultextension='.ktml',
                initialfile=f'{safe_name}.ktml', filetypes=[('KTML save', '*.ktml'), ('All files', '*.*')],
            )
            if dst:
                shutil.copy2(src, dst)
                messagebox.showinfo('Download complete', f'Player KTML saved to:\n{dst}', parent=self)

    def _export_player_sav(self, src, safe_name):
        template = self._sav_template_path
        if not template or not Path(template).exists():
            template = filedialog.askopenfilename(
                parent=self, title='Choose a compatible progress.sav template',
                filetypes=[('TOTK progress save', '*.sav'), ('All files', '*.*')],
            )
            if not template:
                return
            self._sav_template_path = template
        dst = filedialog.asksaveasfilename(
            parent=self, title='Download player progress.sav', defaultextension='.sav',
            initialfile=f'{safe_name}_progress.sav', filetypes=[('TOTK progress save', '*.sav'), ('All files', '*.*')],
        )
        if not dst:
            return
        try:
            result = convert_ktml_to_progress(src, template, dst)
            messagebox.showinfo('Download complete', f'Created:\n{dst}\n\nVariables written: {result.get("total_written", 0):,}', parent=self)
        except Exception as exc:
            messagebox.showerror('Could not export .sav', str(exc), parent=self)

    # Compatibility wrappers retained for any older UI callbacks.
    def export_selected_ktml(self):
        messagebox.showinfo('Player controls moved', 'Use the Download button on the player row and choose .ktml.', parent=self)

    def export_selected_sav(self):
        messagebox.showinfo('Player controls moved', 'Use the Download button on the player row and choose .sav.', parent=self)

    def replace_selected_save(self):
        messagebox.showinfo('Player controls moved', 'Use Upload / Replace on the player row.', parent=self)

    def _set_typed_value(self, doc, section_name, key, value):
        section=doc.setdefault(section_name,{})
        if not isinstance(section,dict):
            raise ValueError(f'KTML section {section_name} is not a mapping.')
        section[key]=value

    def _marc_recovery_presets(self):
        """Load the coordinate presets generated from Marc Robledo's coordinate reference."""
        try:
            data=json.loads(resource_path('totk_coordinate_map.json').read_text(encoding='utf-8'))
            return list(data.get('presets') or [])
        except Exception:
            return []

    def _selected_recovery_position(self, label=None):
        presets=self._marc_recovery_presets()
        chosen=None
        if label:
            for p in presets:
                if p.get('label')==label:
                    chosen=p; break
        if chosen is None:
            for p in presets:
                if 'Lookout Landing' in str(p.get('label','')):
                    chosen=p; break
        if chosen is None:
            chosen={'label':'Lookout Landing (Surface Rescue)','x':-254.12,'y':126.45,'z':-101.60,'layer':'surface'}
        return chosen

    def actor_sync_recovery_tool(self, uid, player_name):
        """Dedicated recovery lab for one client save.

        Soft repair edits the existing player KTML. Deep rebuild starts from Kirbymimi's
        defaultClientSave and copies back only conservative inventory/core-stat fields.
        Factory reset uses the clean Kirbymimi client baseline without copying old state.
        """
        if self._server_running() or not self.realm_paused:
            messagebox.showwarning('Pause realm first','Pause / Stop Realm before opening Actor Sync Recovery Lab.',parent=self)
            return
        target=self._player_save_path(uid)
        if not target:
            messagebox.showerror('Save unavailable','This player save is not mapped yet.',parent=self); return
        top=tk.Toplevel(self); top.title(f'Actor Sync Recovery Lab — {player_name}'); top.configure(bg='#07110F'); top.geometry('760x560'); top.transient(self.winfo_toplevel()); top.grab_set()
        body=ttk.Frame(top,padding=18); body.pack(fill='both',expand=True)
        ttk.Label(body,text='Actor Sync Recovery Lab',style='SectionTitle.TLabel').pack(anchor='w')
        ttk.Label(body,text=f'Player: {player_name}  •  UID: {uid}',style='Gold.TLabel').pack(anchor='w',pady=(2,8))
        ttk.Label(body,text='This tool is linked to the same Marc coordinate preset database used by the World Position Map. Every recovery makes a timestamped backup first.',style='Muted.TLabel',wraplength=700).pack(anchor='w',pady=(0,12))
        preset_var=tk.StringVar()
        presets=self._marc_recovery_presets()
        labels=[p.get('label','') for p in presets if p.get('label')]
        if not labels: labels=['Lookout Landing (Surface Rescue)']
        preset_var.set(labels[0])
        card=ttk.LabelFrame(body,text='Recovery destination',padding=12); card.pack(fill='x',pady=(0,12))
        ttk.Combobox(card,textvariable=preset_var,state='readonly',values=labels,width=55).pack(side='left')
        status=tk.StringVar(value='Choose a recovery depth below.')
        ttk.Label(body,textvariable=status,style='Muted.TLabel',wraplength=700).pack(anchor='w',pady=(0,10))
        btns=ttk.Frame(body); btns.pack(fill='x')
        ttk.Button(btns,text='1  Soft Map Recovery',style='Accent.TButton',command=lambda:self._run_actor_sync_recovery(uid,player_name,'soft',preset_var.get(),status)).pack(fill='x',pady=4)
        ttk.Label(btns,text='Keeps the current client save and repairs position/sequence/respawn state.',style='Muted.TLabel').pack(anchor='w',padx=8)
        ttk.Button(btns,text='2  Deep Client Sync Rebuild (Recommended for persistent fall loop)',command=lambda:self._run_actor_sync_recovery(uid,player_name,'deep',preset_var.get(),status)).pack(fill='x',pady=(12,4))
        ttk.Label(btns,text='Rebuilds from Kirbymimi’s clean defaultClientSave, then restores only pouch inventory, horses, album data, rupees, hearts/stamina/energy and other conservative core stats. This intentionally drops unknown/transient client-state keys that can survive a soft repair.',style='Muted.TLabel',wraplength=690).pack(anchor='w',padx=8)
        ttk.Button(btns,text='3  Factory Client Sync Reset',command=lambda:self._run_actor_sync_recovery(uid,player_name,'factory',preset_var.get(),status)).pack(fill='x',pady=(12,4))
        ttk.Label(btns,text='Most aggressive: replaces the player client KTML with the clean Kirbymimi baseline at the selected Marc map position. Use only after the first two options; a full backup is made automatically.',style='Muted.TLabel',wraplength=690).pack(anchor='w',padx=8)
        ttk.Button(body,text='Close',command=top.destroy).pack(anchor='e',pady=(16,0))

    def _backup_actor_sync_save(self, target, uid, suffix):
        backup_dir=self.workspace/'Backups'/'ActorSyncRecovery'/uid
        backup_dir.mkdir(parents=True,exist_ok=True)
        stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
        backup=backup_dir/f'{target.stem}-{stamp}-{suffix}.ktml'
        shutil.copy2(target,backup)
        return backup

    def _recovery_apply_position(self, doc, preset):
        target_pos={'x':float(preset['x']),'y':float(preset['y']),'z':float(preset['z'])}
        self._set_typed_value(doc,'Vector3','World_PlayerPos',dict(target_pos))
        self._set_typed_value(doc,'Vector3','PlayerStatus.SavePos',dict(target_pos))
        self._set_typed_value(doc,'Float','PlayerStatus.SavePosRadY',0.0)
        self._set_typed_value(doc,'String64','Sequence_CurrentBanc','MainField')
        # Do not add transient DmF_SY flags if the clean client schema does not contain them.
        # If they already exist in a soft-repair save, remove the observed fall-loop keys entirely.
        for sec_name in ('Bool',):
            sec=doc.get(sec_name,{})
            if isinstance(sec,dict):
                sec.pop('HavePlayedEvent.DmF_SY_FallDown',None)
                sec.pop('HavePlayedEvent.DmF_SY_FallDownReturn',None)
                sec.pop('HavePlayedEvent.DmF_SY_GameOver',None)
                sec.pop('HavePlayedEvent.DmF_SY_WarpIn',None)
                sec.pop('HavePlayedEvent.DmF_SY_WarpOut',None)
                sec.pop('HavePlayedEvent.DmF_SY_PadEndAndWarp',None)
        arr=doc.setdefault('String64Array',{}).get('unk3294163435',[])
        if not isinstance(arr,list): arr=[]
        arr=list(arr)
        if len(arr)<16: arr += ['']*(16-len(arr))
        # For non-Lookout presets we deliberately clear return history so the chosen XYZ wins.
        if 'Lookout Landing' in str(preset.get('label','')):
            arr[0]='City_BaseCamp'; arr[1]='BaseCamp_Shelter'
        else:
            arr[0]=''; arr[1]=''
        for i in range(2,len(arr)): arr[i]=''
        self._set_typed_value(doc,'String64Array','unk3294163435',arr)
        if 'Lookout Landing' in str(preset.get('label','')):
            self._set_typed_value(doc,'Bool','IsVisitLocation.City_BaseCamp',True)
            self._set_typed_value(doc,'Bool','IsVisitLocation.BaseCamp_Shelter',True)
        return target_pos

    def _copy_safe_client_state(self, old_doc, clean_doc):
        """Copy only conservative state from an affected client save onto a clean baseline."""
        exact={
            'PlayerStatus.CurrentRupee','PlayerStatus.MaxLife','PlayerStatus.Life','PlayerStatus.MaxStamina',
            'PlayerStatus.MaxEnergy','PlayerStatus.ExtraStamina','PlayerStatus.ExtraEnergy','PlayerStatus.CurrentMamo',
            'PlayerStatus.ExtraLife','PlayerStatus.BreakLife','PlayerStatus.CurrentSpecialPower',
            'Npc_Goddess_UtuwaSum','NpcDemonStatue_UtuwaSum',
        }
        prefixes=('Pouch.','OwnedHorseList.','DeadHorseList.','LastWildHorse.','AlbumData.Photograph.')
        copied=0
        for typ,sec in old_doc.items():
            if not isinstance(sec,dict): continue
            out=clean_doc.setdefault(typ,{})
            if not isinstance(out,dict): continue
            for key,val in sec.items():
                if key in exact or key.startswith(prefixes):
                    out[key]=val; copied+=1
        return copied

    def _run_actor_sync_recovery(self, uid, player_name, mode, preset_label, status_var=None):
        target=self._player_save_path(uid)
        if not target: return
        preset=self._selected_recovery_position(preset_label)
        try:
            old_doc=parse_ktml(target.read_text(encoding='utf-8',errors='strict'))
            backup=self._backup_actor_sync_save(target,uid,f'before-{mode}-recovery')
            if mode in ('deep','factory'):
                clean_path=resource_path('defaultClientSave.ktml')
                clean_doc=parse_ktml(clean_path.read_text(encoding='utf-8',errors='strict'))
                doc=clean_doc
                copied=0
                if mode=='deep': copied=self._copy_safe_client_state(old_doc,doc)
            else:
                doc=old_doc; copied=0
            pos=self._recovery_apply_position(doc,preset)
            normalize_ktml_types(doc); validate_ktml_types(doc)
            serialized=serialize_ktml(doc); parse_ktml(serialized)
            temp=self.workspace/'Backups'/f'.actor-sync-recovery-{uid}.incoming.ktml'
            with temp.open('w',encoding='utf-8',newline='') as f:
                f.write(serialized)
            os.replace(temp,target)
            msg=f'{mode.title()} recovery written at {preset.get("label")}: X {pos["x"]:.2f}, Y {pos["y"]:.2f}, Z {pos["z"]:.2f}. Backup: {backup.name}'
            if mode=='deep': msg+=f' • restored {copied} conservative client values.'
            self._append('[Actor Sync Recovery Lab] '+msg)
            if status_var is not None: status_var.set(msg)
            self.refresh_players()
            messagebox.showinfo('Actor Sync Recovery complete',msg+'\n\nResume the realm and reconnect. If Factory Reset still falls while a brand-new UID works, the failure is outside this player KTML and must be isolated in the client mod/runtime.',parent=self)
        except Exception as exc:
            if status_var is not None: status_var.set('Recovery failed: '+str(exc))
            messagebox.showerror('Actor Sync Recovery failed',str(exc),parent=self)

    def actor_sync_recovery(self, uid, player_name):
        """Repair the verified player-spawn context that can feed the Actor Sync fall loop.

        This operates directly on one mapped User/SaveServer/users/<UID>.ktml file and is only
        available while the realm is paused/stopped. It does not modify Kirbymimi's JAR or
        multiplayer actor code; it repairs the player's saved spawn/fall context before reconnect.
        """
        if self._server_running() or not self.realm_paused:
            messagebox.showwarning(
                'Pause realm first',
                'Pause / Stop Realm before using Actor Sync Recovery.\n\n'
                'The Java/Docker server must be fully stopped so the player KTML is unlocked and '
                'cannot immediately overwrite the recovery while it is being written.',
                parent=self,
            )
            return
        target=self._player_save_path(uid)
        if not target:
            messagebox.showerror('Save unavailable','This player save is not mapped yet.',parent=self)
            return
        if not messagebox.askyesno(
            'Actor Sync Recovery',
            f'Prepare a safe multiplayer spawn recovery for {player_name}?\n\n'
            'This creates a timestamped backup, moves Link to Lookout Landing, resets only the '
            'verified FallDown/FallDownReturn spawn context, forces MainField, resets saved facing '
            'rotation, and refreshes the observed return-location strings.\n\n'
            'Inventory, equipment, rupees, stamina, quests, completion, sages, and normal progression '
            'are not intentionally changed.\n\nContinue?',
            parent=self,
        ):
            return
        try:
            text=target.read_text(encoding='utf-8',errors='strict')
            doc=parse_ktml(text)
            target_pos={'x':-254.12,'y':126.45,'z':-101.60}
            self._set_typed_value(doc,'Vector3','World_PlayerPos',dict(target_pos))
            self._set_typed_value(doc,'Vector3','PlayerStatus.SavePos',dict(target_pos))
            self._set_typed_value(doc,'Float','PlayerStatus.SavePosRadY',0.0)
            self._set_typed_value(doc,'String64','Sequence_CurrentBanc','MainField')
            self._set_typed_value(doc,'Bool','HavePlayedEvent.DmF_SY_FallDown',False)
            self._set_typed_value(doc,'Bool','HavePlayedEvent.DmF_SY_FallDownReturn',False)
            self._set_typed_value(doc,'Bool','IsVisitLocation.City_BaseCamp',True)
            self._set_typed_value(doc,'Bool','IsVisitLocation.BaseCamp_Shelter',True)

            arr=doc.setdefault('String64Array',{}).get('unk3294163435',[])
            if not isinstance(arr,list): arr=[]
            arr=list(arr)
            if len(arr)<20: arr += ['']*(20-len(arr))
            arr[0]='City_BaseCamp'; arr[1]='BaseCamp_Shelter'
            for i in range(2,min(20,len(arr))): arr[i]=''
            self._set_typed_value(doc,'String64Array','unk3294163435',arr)

            normalize_ktml_types(doc)
            validate_ktml_types(doc)
            serialized=serialize_ktml(doc)
            parse_ktml(serialized)  # round-trip parse guard before touching the live player file

            backup_dir=self.workspace/'Backups'/'ActorSyncRecovery'/uid
            backup_dir.mkdir(parents=True,exist_ok=True)
            stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
            backup=backup_dir/f'{target.stem}-{stamp}-before-actor-sync-recovery.ktml'
            shutil.copy2(target,backup)
            temp=self.workspace/'Backups'/f'.actor-sync-recovery-{uid}.incoming.ktml'
            temp.parent.mkdir(parents=True,exist_ok=True)
            with temp.open('w',encoding='utf-8',newline='') as f:
                f.write(serialized)
            os.replace(temp,target)

            self._append(f'[Actor Sync Recovery] {player_name}: safe Lookout Landing spawn context written; backup: {backup.name}')
            self.refresh_players()
            messagebox.showinfo(
                'Actor Sync Recovery complete',
                f'{player_name} has been prepared for a clean multiplayer respawn.\n\n'
                f'Backup:\n{backup}\n\n'
                'Next: Resume Realm, reconnect, and watch the console. If the fall loop still begins '
                'only after MainField, the remaining fault is inside the multiplayer Actor Sync runtime '
                'rather than the saved spawn context.',
                parent=self,
            )
        except Exception as exc:
            messagebox.showerror('Actor Sync Recovery failed',f'No recovery was intentionally committed unless validation completed.\n\n{exc}',parent=self)

    def replace_player_save(self, uid, player_name):
        if self._server_running():
            messagebox.showwarning(
                'Stop the realm first',
                'Pause / Stop Realm first. In Docker mode this fully stops the container, releases the port, and unlocks the player files while preserving the room and countdown. Then upload the save and press Resume Realm.',
                parent=self,
            )
            return
        target = self._user_file_for_uid(uid)
        if not target:
            messagebox.showerror('Save unavailable', 'This player save is not mapped yet.', parent=self)
            return
        src = filedialog.askopenfilename(
            parent=self, title=f'Upload / replace save for {player_name}',
            filetypes=[('Supported saves', '*.ktml *.sav'), ('KTML save', '*.ktml'), ('TOTK progress save', '*.sav'), ('All files', '*.*')],
        )
        if not src:
            return
        src = Path(src)
        # IMPORTANT: never place backup folders inside User/SaveServer/users.
        # Kirbymimi's SaveServer enumerates every child there and attempts to
        # decode it as a player save. Backups live outside the scanned resource.
        backup_dir = self.workspace / 'Backups' / 'PlayerSaves' / target.stem
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f'{target.stem}_{datetime.now().strftime("%Y%m%d-%H%M%S")}.ktml'

        temp_dir = self.workspace / 'Temp'
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp = temp_dir / f'{target.stem}.incoming.ktml'
        try:
            shutil.copy2(target, backup)
            if src.suffix.lower() == '.ktml':
                text = src.read_text(encoding='utf-8', errors='strict')
                parse_ktml(text)
                temp.write_text(text, encoding='utf-8')
            elif src.suffix.lower() == '.sav':
                convert_progress_to_ktml(src, target, temp)
                parse_ktml(temp.read_text(encoding='utf-8', errors='strict'))
            else:
                raise ValueError('Choose a .ktml or .sav file.')
            # Windows can keep the old player KTML locked for a short moment
            # after Java stops. Retry the atomic replacement instead of failing
            # immediately with WinError 5 (Access denied).
            replace_error = None
            for _ in range(24):
                try:
                    os.replace(temp, target)
                    replace_error = None
                    break
                except PermissionError as exc:
                    replace_error = exc
                    time.sleep(0.25)
            if replace_error is not None:
                raise PermissionError(
                    f'The player save is still locked by another process after waiting 6 seconds. '
                    f'Close any old Sacred Realms/Java server process and try again. Original error: {replace_error}'
                )
            self._append(f'Replaced player save {target.name}; automatic backup: {backup.name}')
            self.refresh_players()
            messagebox.showinfo(
                'Player save replaced',
                f'{player_name} now has the uploaded save.\n\nBackup created:\n{backup}\n\nPress Resume Realm when you are ready to continue.',
                parent=self,
            )
        except Exception as exc:
            try:
                if temp.exists():
                    temp.unlink()
            except Exception:
                pass
            messagebox.showerror('Save replacement failed', f'The original was backed up before replacement was attempted.\n\n{exc}', parent=self)

    # ------------------------------------------------------- misc controls
    def open_folder(self):
        target = self.workspace if self.workspace and self.workspace.exists() else self.root_workspace
        target.mkdir(parents=True, exist_ok=True)
        if os.name == 'nt':
            os.startfile(target)
        else:
            subprocess.Popen(['xdg-open', str(target)])

    def _lan_ip(self):
        """Return the IPv4 selected by Windows' current default route."""
        try:
            s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(('1.1.1.1',80)); ip=s.getsockname()[0]; s.close(); return ip
        except Exception:
            return '(could not detect)'

    def _windows_adapter_ipv4(self):
        """Return [(ip, adapter, profile)] using PowerShell when available."""
        rows=[]
        if os.name!='nt': return rows
        ps=(
            "$ErrorActionPreference='SilentlyContinue'; "
            "$ips=Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.IPAddress -ne '127.0.0.1' -and $_.IPAddress -ne '0.0.0.0'}; "
            "$rows=foreach($i in $ips){$a=Get-NetAdapter -InterfaceIndex $i.InterfaceIndex; $p=Get-NetConnectionProfile -InterfaceIndex $i.InterfaceIndex; "
            "[PSCustomObject]@{IP=$i.IPAddress;Alias=$i.InterfaceAlias;Description=$a.InterfaceDescription;Status=$a.Status;Category=$p.NetworkCategory}}; $rows | ConvertTo-Json -Compress"
        )
        try:
            out=subprocess.check_output(['powershell','-NoProfile','-Command',ps],text=True,errors='ignore',creationflags=subprocess.CREATE_NO_WINDOW,timeout=8).strip()
            if not out: return rows
            data=json.loads(out); data=data if isinstance(data,list) else [data]
            for r in data:
                ip=str(r.get('IP','')).strip(); alias=str(r.get('Alias','')).strip(); desc=str(r.get('Description','')).strip(); cat=str(r.get('Category','')).strip()
                if re.fullmatch(r'\d+(?:\.\d+){3}',ip): rows.append((ip,alias or desc,cat))
        except Exception:
            pass
        return rows

    def _detected_ipv4_addresses(self):
        """Return rich adapter records and prioritize Radmin/Hamachi/Tailscale-like VPNs."""
        records=[]; seen=set()
        def add(ip,adapter='',profile=''):
            ip=(ip or '').strip()
            if not re.fullmatch(r'\d+(?:\.\d+){3}',ip) or ip.startswith('127.') or ip=='0.0.0.0' or ip in seen: return
            seen.add(ip); records.append({'ip':ip,'adapter':adapter or 'Network adapter','profile':profile or ''})
        for ip,ad,prof in self._windows_adapter_ipv4(): add(ip,ad,prof)
        route=self._lan_ip()
        if not route.startswith('('): add(route,'Default route','')
        try:
            for info in socket.getaddrinfo(socket.gethostname(),None,socket.AF_INET,socket.SOCK_DGRAM): add(info[4][0],'Host adapter','')
        except Exception: pass
        if os.name!='nt':
            try:
                for token in subprocess.check_output(['hostname','-I'],text=True,errors='ignore').split(): add(token,'Host adapter','')
            except Exception: pass
        def rank(r):
            text=(r['adapter']+' '+r['ip']).lower()
            if 'radmin' in text or r['ip'].startswith('26.'): return (0,r['adapter'],r['ip'])
            if any(x in text for x in ('hamachi','tailscale','zerotier','wireguard','vpn')): return (1,r['adapter'],r['ip'])
            if r['ip']==route: return (2,r['adapter'],r['ip'])
            return (3,r['adapter'],r['ip'])
        records.sort(key=rank)
        return records

    def refresh_primary_ip(self):
        records=self._detected_ipv4_addresses(); self._ip_option_map={}
        values=[]
        for r in records:
            tag='RADMIN VPN' if ('radmin' in r['adapter'].lower() or r['ip'].startswith('26.')) else ('VPN / VIRTUAL' if any(x in r['adapter'].lower() for x in ('vpn','hamachi','tailscale','zerotier','wireguard')) else 'LOCAL')
            profile=f" • {r['profile']}" if r['profile'] else ''
            label=f"{r['ip']}  —  {r['adapter']}  [{tag}{profile}]"
            self._ip_option_map[label]=r; values.append(label)
        if hasattr(self,'primary_ip_combo'): self.primary_ip_combo.configure(values=values)
        current=self.primary_ip_var.get().strip() if hasattr(self,'primary_ip_var') else ''
        # Never force Radmin over a user's choice. Preserve an exact selection or a
        # manually typed IPv4 address. Radmin is selected only by Radmin Direct Mode.
        selected=None
        if current in self._ip_option_map:
            selected=current
        elif re.fullmatch(r'\d{1,3}(?:\.\d{1,3}){3}', current):
            selected=current
        if selected is None:
            selected=next((v for v in values if self._ip_option_map[v].get('ip')==route),None) or (values[0] if values else '(could not detect)')
        self.primary_ip_var.set(selected)
        if selected in self._ip_option_map:
            r=self._ip_option_map[selected]
            if 'radmin' in r['adapter'].lower() or r['ip'].startswith('26.'):
                self.primary_ip_label_var.set('Radmin detected. Friends must join the same Radmin network and connect to this 26.x address; Windows Firewall must allow the realm port on this adapter.')
            else:
                self.primary_ip_label_var.set(f"Selected adapter: {r['adapter']}"+(f" • profile {r['profile']}" if r['profile'] else ''))
        else: self.primary_ip_label_var.set('No usable IPv4 adapter detected.')

    def _connection_ip(self):
        value=self.primary_ip_var.get().strip() if hasattr(self,'primary_ip_var') else ''
        if value in getattr(self,'_ip_option_map',{}): return self._ip_option_map[value]['ip']
        m=re.match(r'(\d+(?:\.\d+){3})',value)
        return m.group(1) if m else self._lan_ip()

    def _selected_adapter_record(self):
        return getattr(self,'_ip_option_map',{}).get(self.primary_ip_var.get().strip())

    def use_radmin_direct_mode(self):
        """Configure the safest known host path for Radmin VPN.

        Docker Desktop port publishing can be unreachable through some Windows virtual/VPN
        adapters even when it works from the physical LAN. Kirbymimi's native Java server
        uses ServerSocket(port), so running it directly on Windows avoids that Docker/NAT
        layer and lets Windows bind the listener to all local interfaces, including Radmin.
        """
        records=self._detected_ipv4_addresses()
        radmin=next((r for r in records if 'radmin' in r['adapter'].lower() or r['ip'].startswith('26.')),None)
        if not radmin:
            messagebox.showwarning('Radmin VPN not detected','Sacred Zonai Realms could not find a Radmin VPN adapter/26.x address. Open Radmin VPN, join the same Radmin network as the other player, then click Refresh IP.',parent=self)
            return
        # Keep the exact selectable label when possible, otherwise allow a typed 26.x address.
        label=next((k for k,v in getattr(self,'_ip_option_map',{}).items() if v.get('ip')==radmin['ip']),radmin['ip'])
        self.primary_ip_var.set(label)
        self.host_engine_var.set('Native Java (Legacy)')
        self.primary_ip_label_var.set(
            f"Radmin Direct Mode ready: {radmin['ip']} on {radmin['adapter']}. Native Java avoids Docker's VPN/NAT path. "
            "Both players must be in the same Radmin network. Allow the selected realm TCP port through Windows Firewall."
        )
        messagebox.showinfo(
            'Radmin Direct Mode',
            f"Selected Radmin address: {radmin['ip']}\nAdapter: {radmin['adapter']}\nHosting engine: Native Java (Legacy)\n\n"
            "Why this mode exists: Docker Desktop can publish a port successfully to your normal LAN address while the same published port is unreachable through a virtual VPN adapter. Native Java removes that extra forwarding layer.\n\n"
            "Next: open/resume the realm, click Allow Port in Firewall, then give the other Radmin member the 26.x address and realm port.",
            parent=self,
        )

    def vpn_diagnostics(self):
        """Check the selected VPN adapter and local listener without changing the system."""
        rec=self._selected_adapter_record(); ip=self._connection_ip(); port=self.port_var.get().strip()
        lines=[f'Selected IP: {ip}']
        if rec: lines += [f"Adapter: {rec['adapter']}",f"Windows network profile: {rec['profile'] or 'Unknown'}"]
        radmin=bool(rec and ('radmin' in rec['adapter'].lower() or rec['ip'].startswith('26.')))
        lines.append('Radmin-style adapter: '+('YES' if radmin else 'No'))
        if port.isdigit() and self._server_running():
            listen=False
            try:
                out=subprocess.check_output(['netstat','-ano','-p','tcp'],text=True,errors='ignore',creationflags=(subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0))
                pat=re.compile(r'\s(?:0\.0\.0\.0|'+re.escape(ip)+r'):'+re.escape(port)+r'\s+.*LISTENING',re.I)
                listen=bool(pat.search(out))
            except Exception: pass
            lines.append(f'Local port {port} listening on all/selected interfaces: '+('YES' if listen else 'NOT DETECTED'))
        else: lines.append('Open or resume a realm to test the listening port.')
        if radmin:
            lines.append('Radmin requirement: the joining player must be in the SAME Radmin VPN network and use the host 26.x IP shown above.')
            if self._using_docker():
                lines.append('IMPORTANT: Docker Desktop may be reachable from the physical LAN but not from some virtual VPN adapters. Use Radmin Direct Mode to run Kirbymimi native Java directly on Windows for this test.')
            lines.append('Kirbymimi server binding: server.jar uses Java ServerSocket(port), which binds to the wildcard host interface; v5.70 Docker also explicitly publishes 0.0.0.0:port. Selecting the Radmin IP changes the advertised connection address, not the Java bind.')
            lines.append('If the main LAN IP works but the Radmin IP does not, the most common remaining blocker is Windows Firewall/network profile or Radmin peer connectivity.')
        messagebox.showinfo('VPN / Radmin diagnostics','\n'.join(lines),parent=self)

    def allow_realm_port_firewall(self):
        """Ask Windows for elevation and add a narrow inbound TCP rule for the current realm port."""
        if os.name!='nt':
            messagebox.showinfo('Windows only','Automatic firewall-rule creation is available on Windows only.',parent=self); return
        port=self.port_var.get().strip()
        if not port.isdigit(): messagebox.showwarning('Open a realm first','Open a realm so Sacred Realms knows which port to allow.',parent=self); return
        if not messagebox.askyesno('Allow realm port?',f'Create a Windows Defender Firewall inbound TCP rule for Sacred Realms port {port}?\n\nWindows will ask for Administrator approval. This does not disable the firewall.',parent=self): return
        rule=f'Sacred Realms TOTK {port}'
        cmd=f"New-NetFirewallRule -DisplayName '{rule}' -Direction Inbound -Action Allow -Protocol TCP -LocalPort {port} -Profile Any"
        try:
            arg=f'-NoProfile -ExecutionPolicy Bypass -Command "{cmd}"'
            rc=subprocess.run(['powershell','-NoProfile','-Command',f"Start-Process powershell -Verb RunAs -ArgumentList '{arg}' -Wait"],creationflags=subprocess.CREATE_NO_WINDOW).returncode
            messagebox.showinfo('Firewall helper','Windows firewall rule request completed. Run VPN Diagnostics again, then have the other Radmin peer connect to the host Radmin IP and realm port.',parent=self)
        except Exception as exc:
            messagebox.showerror('Firewall helper failed',str(exc),parent=self)

    def connection_info(self):
        if not self.workspace:
            messagebox.showinfo('Connection info', 'Open a realm first. The app will then show the realm IP, random port, and countdown.', parent=self)
            return
        info = (
            f'Realm: {self.active_room_name.get()}\n'
            f'Primary / VPN IP: {self._connection_ip()}\n'
            f'Port: {self.port_var.get()}\n'
            f'Time remaining: {self.countdown_var.get()}\n'
            f'PvP: {"Enabled" if self.pvp_enabled_var.get() else "Disabled"}\n'
            f'Max upload: {self.upload_speed_var.get()} bytes/sec\n'
            f'Hosting engine: {self.host_engine_var.get()}\n\n'
            'VPN note: connect the VPN before opening the realm and click Refresh IP. Select or type the reachable VPN address your players should use. If you use a normal LAN/public-IP setup, the selected port still needs to be reachable through the router/firewall.'
        )
        try:
            self.clipboard_clear()
            self.clipboard_append(f'{self._connection_ip()}:{self.port_var.get()}')
            info += '\n\nIP:port copied to the clipboard.'
        except Exception:
            pass
        messagebox.showinfo('Realm connection', info, parent=self)

    def run_diagnostics(self):
        self._append('=== REALM DIAGNOSTICS ===')
        results = []
        if self._using_docker():
            docker = self._docker_cli()
            results.append(('Docker CLI found', bool(docker), docker or 'not found'))
            results.append(('Docker engine ready', self._docker_ready(show_error=False), self.host_engine_var.get()))
            results.append(('Docker container', bool(self.container_name and self._docker_container_exists()), self.container_name or 'not created'))
        else:
            java = shutil.which('java')
            results.append(('Java found', bool(java), java or 'not found'))
        results.append(('Active realm', bool(self.workspace and self.workspace.exists()), str(self.workspace or 'none')))
        if self.workspace:
            results.append(('server.jar', (self.workspace / 'server.jar').exists(), str(self.workspace / 'server.jar')))
            creator = self.creator_file()
            results.append(('serverCreator.ktml', bool(creator and creator.exists()), str(creator)))
            default = self.default_client_save()
            results.append(('defaultClientSave.ktml', bool(default and default.exists() and default.stat().st_size > 1000), str(default)))
            for rel in ['User/SaveServer/save.ktml', 'Resources/SaveServer/name2hash.ktml', 'Resources/SaveServer/bitKeyMap.ktml', 'Resources/TOTKServer/actorList.ktml', 'Resources/SyncedActorServer/actorCreators.ktml']:
                p = self.workspace / rel
                results.append((rel, p.exists() and p.stat().st_size > 0, str(p)))
            if creator and creator.exists():
                ctext = creator.read_text(encoding='utf-8', errors='ignore')
                expected = self.upload_speed_var.get().replace(',', '').strip()
                if expected == str(self.DEFAULT_UPLOAD_BPS):
                    # At the default we intentionally do not inject the property;
                    # the JAR's built-in default is the compatibility baseline.
                    configured = ('"serverMaxUploadPerSecond"' not in ctext) or (f'"serverMaxUploadPerSecond": {expected}' in ctext)
                    detail = f'{expected} bytes/sec (built-in default; config may remain untouched)'
                else:
                    configured = f'"serverMaxUploadPerSecond": {expected}' in ctext
                    detail = expected
                results.append(('Upload limit configured', configured, detail))
        try:
            port = int(self.port_var.get())
            port_ok = self.PORT_MIN <= port <= self.PORT_MAX
        except Exception:
            port_ok = False
        results.append(('Assigned port range', port_ok, self.port_var.get()))
        results.append(('Server process', self._server_running(), self.status.get()))

        for label, ok, detail in results:
            self._append(f"{'[PASS]' if ok else '[FAIL]'} {label}: {detail}")
        failed = [x for x in results if not x[1]]
        if failed:
            messagebox.showwarning('Diagnostics', f'{len(failed)} check(s) need attention. See the Live Realm Console.', parent=self)
        else:
            messagebox.showinfo('Diagnostics', 'All local realm checks passed.', parent=self)

    def black_screen_help(self):
        messagebox.showinfo(
            'Black screen after connecting',
            'The message "<player> connected" proves only that the network handshake reached the Java server. '
            "Kirbymimi's current console output does not give this launcher a dependable \"game finished loading\" event.\n\n"
            'BEST BASELINE TEST:\n'
            '1. Close the current realm.\n'
            '2. Click Reset to Kirbymimi Defaults.\n'
            '3. Open a brand-new realm. At 1,000,000 bytes/sec (1 MB/s recommended) + PvP enabled, v5.40 keeps the tested 1 MB/s baseline and can run the realm inside Docker Desktop; only the random port is changed.\n'
            '4. Use the same 1.2.1 client/exefs build that works with Zonai Hosting.\n'
            '5. Test before editing or replacing any client save.\n'
            '6. Watch the Live Realm Console. If it prints "<player> left", the launcher will now immediately show Disconnected.\n\n'
            'IMPORTANT: if the exact same Switch/client works on Zonai Hosting but still black-screens against this untouched local baseline, the strongest remaining suspect is a server-build/runtime mismatch rather than the TCP connection itself. '
            'For save editing, Docker Desktop mode is recommended: Stop Realm fully stops the container, releases the exact port, and unlocks bind-mounted player saves; Resume uses the SAME port.',
            parent=self,
        )
