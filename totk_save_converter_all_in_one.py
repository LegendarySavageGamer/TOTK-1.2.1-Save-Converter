import os
import sys
import traceback
import platform
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

try:
    from PIL import Image, ImageTk
except Exception:
    Image = ImageTk = None

from totk_client_editor import ClientSaveEditorFrame
from totk_server_manager import ServerManagerFrame

try:
    import winsound
except ImportError:
    winsound = None

from wesley_zonai_save_converter import ktml_to_progress_sav as zonai_ktml_to_sav, progress_sav_to_ktml as zonai_sav_to_ktml

from totk_converter_core import (
    convert_ktml_to_progress,
    convert_progress_to_ktml,
    inspect_template,
    parse_ktml,
)

# ============================================================
# EASY CUSTOMIZATION SECTION
# Change these values later without touching the converter logic.
# ============================================================
APP_NAME = "SACRED ZONAI REALMS"
APP_VERSION = "Version 7.26 — QR DIAGNOSTICS & ERROR SHARING"
APP_CREDIT = "Sacred Zonai Realms by ThyHeroOfTime  •  Community credits and special thanks in the Credits tab"

# Files bundled with the program.
WINDOW_ICON_ICO = "zonai_realm.ico"
WINDOW_ICON_PNG = "zonai_realm.png"
SUCCESS_SOUND_FILE = "conversion_success.wav"
ERROR_SOUND_FILE = "conversion_error.wav"
WARNING_SOUND_FILE = "warning_popup.wav"

# Dark theme colors.
BG = "#07110F"
PANEL = "#0D1B18"
PANEL_2 = "#132620"
CARD = "#10201C"
TEXT = "#F4F1E8"
MUTED = "#9BAAA4"
ACCENT = "#D8B85B"
ACCENT_HOVER = "#E7CC78"
TEAL = "#6EB5A4"
BORDER = "#294139"
ERROR = "#E9877C"
SUCCESS = "#7BC59A"
# ============================================================




def resource_path(relative_name):
    """Find bundled resources both from source and a PyInstaller EXE."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative_name


class RoundedTabBar(tk.Frame):
    """Canvas-based navigation pills so the selected tab has real rounded corners."""
    def __init__(self, parent, bg=BG):
        super().__init__(parent, bg=bg, bd=0, highlightthickness=0)
        self._tabs = []
        self._active = None

    @staticmethod
    def _round_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
        r = min(radius, (x2-x1)//2, (y2-y1)//2)
        points = [x1+r,y1, x2-r,y1, x2,y1, x2,y1+r, x2,y2-r, x2,y2,
                  x2-r,y2, x1+r,y2, x1,y2, x1,y2-r, x1,y1+r, x1,y1]
        return canvas.create_polygon(points, smooth=True, splinesteps=24, **kwargs)

    def add(self, label, frame):
        width = max(145, min(250, 26 + len(label) * 8))
        c = tk.Canvas(self, width=width, height=44, bg=BG, bd=0, highlightthickness=0, cursor='hand2')
        c.pack(side='left', padx=(0, 7))
        tab = {'label': label, 'frame': frame, 'canvas': c}
        self._tabs.append(tab)
        c.bind('<Button-1>', lambda e, t=tab: self.select(t))
        c.bind('<Enter>', lambda e, t=tab: self._draw(t, hover=True))
        c.bind('<Leave>', lambda e, t=tab: self._draw(t))
        self._draw(tab)
        if self._active is None:
            self.select(tab)

    def _draw(self, tab, hover=False):
        c = tab['canvas']; c.delete('all')
        active = tab is self._active
        fill = ACCENT if active else ('#17342C' if hover else PANEL_2)
        outline = ACCENT if active else BORDER
        fg = '#132019' if active else (TEXT if hover else MUTED)
        self._round_rect(c, 2, 3, int(c['width'])-2, 41, 13, fill=fill, outline=outline, width=1)
        c.create_text(int(c['width'])/2, 22, text=tab['label'], fill=fg, font=('Segoe UI', 10, 'bold'))

    def select(self, tab):
        if tab is self._active:
            self._animate_click(tab)
            return
        self._active = tab
        for item in self._tabs:
            if item is tab:
                item['frame'].tkraise()
            self._draw(item)
        self._animate_click(tab)

    def _animate_click(self, tab, step=0):
        """Small Zelda-like gold shimmer whenever a main tab is selected."""
        if tab is not self._active or not tab['canvas'].winfo_exists():
            return
        c = tab['canvas']
        self._draw(tab)
        width = int(c['width'])
        # A short moving rune/glint; event-driven only, so it adds no idle CPU load.
        x = 8 + int((max(1, width - 16)) * (step / 10.0))
        c.create_line(x, 10, x, 34, fill='#FFF0A8', width=2, tags='tab_shimmer')
        c.create_oval(x-3, 19, x+3, 25, fill='#FFF4C5', outline='', tags='tab_shimmer')
        if step < 10:
            c.after(18, lambda: self._animate_click(tab, step + 1))
        else:
            c.after(35, lambda: self._draw(tab))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} — {APP_VERSION}")
        self.geometry("1480x920")
        self.minsize(1180, 760)
        self.minsize(1000, 700)
        self.configure(bg=BG)
        self._set_window_icon()

        self.status_var = tk.StringVar(value="Ready.")
        self.sound_enabled_var = tk.BooleanVar(value=True)

        # KTML -> SAV variables
        self.to_sav_ktml_var = tk.StringVar()
        self.to_sav_template_var = tk.StringVar()
        self.to_sav_output_var = tk.StringVar()

        # SAV -> KTML variables
        self.to_ktml_sav_var = tk.StringVar()
        self.to_ktml_template_var = tk.StringVar()
        self.to_ktml_output_var = tk.StringVar()

        self._setup_dark_theme()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)



    def _on_close(self):
        """Stop the managed server cleanly before closing the desktop app."""
        try:
            manager = getattr(self, "server_manager", None)
            if manager is not None:
                manager.stop()
        except Exception:
            pass
        self.destroy()

    def _set_window_icon(self):
        """
        Replace Tkinter's default feather/leaf-style window icon with the
        bundled Triforce icon.
        """
        try:
            ico = resource_path(WINDOW_ICON_ICO)
            if ico.exists():
                self.iconbitmap(default=str(ico))
                return
        except Exception:
            pass

        # PNG fallback (also useful when running outside Windows).
        try:
            png = resource_path(WINDOW_ICON_PNG)
            if png.exists():
                self._window_icon_image = tk.PhotoImage(file=str(png))
                self.iconphoto(True, self._window_icon_image)
        except Exception:
            pass

    def _play_wav(self, filename, fallback="bell"):
        """
        Play one bundled WAV file asynchronously.

        To change a sound later, replace the WAV file or change the filename
        in the EASY CUSTOMIZATION SECTION near the top of this source file.
        """
        if not self.sound_enabled_var.get():
            return

        sound_path = resource_path(filename)

        try:
            if winsound is not None and sound_path.exists():
                # Stop a previous app sound first, then play the new one.
                winsound.PlaySound(None, winsound.SND_PURGE)
                winsound.PlaySound(
                    str(sound_path),
                    winsound.SND_FILENAME | winsound.SND_ASYNC,
                )
                return

            # Fallbacks if the bundled file cannot be found.
            if winsound is not None:
                if fallback == "error":
                    winsound.MessageBeep(winsound.MB_ICONHAND)
                else:
                    winsound.MessageBeep(winsound.MB_OK)
            else:
                self.bell()
        except Exception:
            try:
                self.bell()
            except Exception:
                pass

    def _play_success_sound(self):
        """Play the configured successful-conversion sound."""
        self._play_wav(SUCCESS_SOUND_FILE, fallback="success")

    def _play_error_sound(self):
        """Play the configured failed-conversion/error sound."""
        self._play_wav(ERROR_SOUND_FILE, fallback="error")

    def _play_warning_sound(self):
        """Play the configured warning-popup sound."""
        self._play_wav(WARNING_SOUND_FILE, fallback="error")

    def _setup_dark_theme(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL_2, font=("Segoe UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("Card.TFrame", background=CARD, relief="flat")
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Title.TLabel", background=BG, foreground="#F6E8B2", font=("Georgia", 25, "bold"))
        style.configure("SectionTitle.TLabel", background=BG, foreground="#F4F1E8", font=("Georgia", 15, "bold"))
        style.configure("Eyebrow.TLabel", background=CARD, foreground=TEAL, font=("Segoe UI", 8, "bold"))
        style.configure("Gold.TLabel", background=BG, foreground=ACCENT, font=("Segoe UI", 10, "bold"))
        style.configure("CardGold.TLabel", background=CARD, foreground=ACCENT, font=("Segoe UI", 12, "bold"))
        style.configure("Hero.TLabel", background=BG, foreground=ACCENT, font=("Georgia", 18, "bold"))
        style.configure("Rune.TLabel", background=BG, foreground=TEAL, font=("Georgia", 11, "bold"))
        style.configure("CreditTitle.TLabel", background=CARD, foreground=ACCENT, font=("Georgia", 14, "bold"))
        style.configure("CreditBody.TLabel", background=CARD, foreground=TEXT, font=("Segoe UI", 10))

        style.configure("TLabelframe", background=PANEL, foreground=TEXT, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, relief="solid")
        style.configure("TLabelframe.Label", background=PANEL, foreground=ACCENT, font=("Segoe UI", 10, "bold"))
        style.configure("TEntry", fieldbackground=PANEL_2, foreground=TEXT, insertcolor=TEXT, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, padding=8)
        style.configure("TCombobox", fieldbackground=PANEL_2, background=PANEL_2, foreground=TEXT, arrowcolor=ACCENT, bordercolor=BORDER, padding=6)

        style.configure("TButton", background=PANEL_2, foreground=TEXT, bordercolor=BORDER, padding=(12, 8), font=("Segoe UI", 9, "bold"), relief="flat")
        style.map("TButton", background=[("active", "#1A352D"), ("pressed", "#10261F")], foreground=[("disabled", "#6E7A76")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#132019", bordercolor=ACCENT, padding=(16, 9), font=("Segoe UI", 10, "bold"), relief="flat")
        style.map("Accent.TButton", background=[("active", ACCENT_HOVER), ("pressed", "#C9A54C")])

        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 8, 0, 0))
        style.configure("TNotebook.Tab", background=PANEL_2, foreground=MUTED, padding=(18, 10), font=("Georgia", 10, "bold"), borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", ACCENT)], foreground=[("selected", "#132019")])

        style.configure("Treeview", background="#0B1714", fieldbackground="#0B1714", foreground=TEXT, rowheight=27, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        style.configure("Treeview.Heading", background="#173028", foreground=ACCENT, font=("Segoe UI", 9, "bold"), relief="flat")
        style.map("Treeview", background=[("selected", "#31584B")], foreground=[("selected", "#FFFFFF")])

    def _build_ui(self):
        outer = ttk.Frame(self, padding=(22, 18, 22, 14))
        outer.pack(fill="both", expand=True)

        # Graphic Hylian header. The generated artwork is decorative only; all controls remain native widgets.
        self._header_canvas = tk.Canvas(outer, height=150, bg=BG, bd=0, highlightthickness=1, highlightbackground=ACCENT)
        self._header_canvas.pack(fill="x", pady=(0, 14))
        self._header_source = None
        self._header_photo = None
        self._zonai_logo_source = None
        self._zonai_logo_photo = None
        try:
            lp = resource_path(WINDOW_ICON_PNG)
            if Image is not None and lp.exists(): self._zonai_logo_source = Image.open(lp).convert("RGBA")
        except Exception: self._zonai_logo_source = None
        try:
            art = resource_path("assets/theme/hylian_header.png")
            if Image is not None and art.exists():
                self._header_source = Image.open(art).convert("RGB")
        except Exception:
            self._header_source = None

        def redraw_header(_evt=None):
            c=self._header_canvas
            w=max(900,c.winfo_width()); h=max(150,c.winfo_height())
            c.delete('all')
            if self._header_source is not None and ImageTk is not None:
                im=self._header_source.copy()
                # center crop to the current header aspect ratio
                src_ratio=im.width/im.height; dst_ratio=w/h
                if src_ratio>dst_ratio:
                    nw=int(im.height*dst_ratio); x=(im.width-nw)//2; im=im.crop((x,0,x+nw,im.height))
                else:
                    nh=int(im.width/dst_ratio); y=(im.height-nh)//2; im=im.crop((0,y,im.width,y+nh))
                im=im.resize((w,h),Image.LANCZOS)
                self._header_photo=ImageTk.PhotoImage(im)
                c.create_image(0,0,image=self._header_photo,anchor='nw')
                c.create_rectangle(0,0,w,h,fill='#07110F',stipple='gray50',outline='')
            c.create_text(32,38,anchor='w',text=APP_NAME.upper(),fill='#F6E8B2',font=('Georgia',26,'bold'))
            c.create_text(34,72,anchor='w',text='SAVE CONVERSION  •  CLIENT EDITOR  •  ZONAI REALMS HOSTING',fill=TEAL,font=('Segoe UI',9,'bold'))
            c.create_text(34,104,anchor='w',text='CONVERT   ✦   EDIT   ✦   RESTORE   ✦   PLAY TOGETHER',fill=ACCENT,font=('Georgia',11,'bold'))
            c.create_text(w-28,42,anchor='e',text='A GREATER HYRULE AWAITS',fill='#F6E8B2',font=('Georgia',10,'bold'))
            c.create_text(w-28,69,anchor='e',text=APP_VERSION,fill=TEAL,font=('Segoe UI',9,'bold'))
            # User-supplied Zonai crest replaces the old Triforce branding.
            if self._zonai_logo_source is not None and ImageTk is not None:
                from PIL import ImageFilter
                logo=self._zonai_logo_source.copy(); logo.thumbnail((78,78),Image.LANCZOS)
                pad=20; glow=Image.new('RGBA',(logo.width+pad*2,logo.height+pad*2),(0,0,0,0)); glow.paste(logo,(pad,pad),logo)
                alpha=glow.getchannel('A').filter(ImageFilter.GaussianBlur(9)); aura=Image.new('RGBA',glow.size,(72,230,190,0)); aura.putalpha(alpha.point(lambda a:min(185,a)))
                aura.alpha_composite(glow); self._zonai_logo_photo=ImageTk.PhotoImage(aura)
                c.create_image(w-120,105,image=self._zonai_logo_photo,anchor='center')
        self._header_canvas.bind('<Configure>',redraw_header)
        self.after(60,redraw_header)

        nav_wrap = ttk.Frame(outer)
        nav_wrap.pack(fill='x', pady=(0, 10))
        self.nav = RoundedTabBar(nav_wrap)
        self.nav.pack(anchor='w')

        content = tk.Frame(outer, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        content.pack(fill='both', expand=True)
        content.grid_rowconfigure(0, weight=1); content.grid_columnconfigure(0, weight=1)

        self.to_sav_tab = ttk.Frame(content, padding=18, style="Panel.TFrame")
        self.to_ktml_tab = ttk.Frame(content, padding=18, style="Panel.TFrame")
        self.editor_tab = ttk.Frame(content, padding=0, style="Panel.TFrame")
        self.server_tab = ttk.Frame(content, padding=0, style="Panel.TFrame")
        self.world_position_tab = ttk.Frame(content, padding=0, style="Panel.TFrame")
        self.credits_tab = ttk.Frame(content, padding=0, style="Panel.TFrame")
        self.error_reports_tab = ttk.Frame(content, padding=0, style="Panel.TFrame")
        for frame in (self.to_sav_tab, self.to_ktml_tab, self.editor_tab, self.server_tab, self.world_position_tab, self.credits_tab, self.error_reports_tab):
            frame.grid(row=0, column=0, sticky='nsew')

        self.nav.add("◇  KTML  →  SAV", self.to_sav_tab)
        self.nav.add("◇  SAV  →  KTML", self.to_ktml_tab)
        self.nav.add("⌖  Client Save Editor", self.editor_tab)
        self.nav.add("△  Zonai Realms Hosting", self.server_tab)
        self.nav.add("⌖  World Position Map", self.world_position_tab)
        self.nav.add("✦  Credits", self.credits_tab)
        self.nav.add("⚠  Error Reports", self.error_reports_tab)

        self._build_to_sav_tab()
        self._build_to_ktml_tab()
        self._build_credits_tab()
        self._build_error_reports_tab()
        self.client_editor = ClientSaveEditorFrame(self.editor_tab, bg=BG, muted=MUTED, accent=ACCENT)
        self.client_editor.pack(fill="both", expand=True)
        self.client_editor.attach_world_position_tab(self.world_position_tab)
        self.server_manager = ServerManagerFrame(self.server_tab, muted=MUTED, accent=ACCENT)
        self.server_manager.pack(fill="both", expand=True)

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Label(footer, text="⚠  Always back up your original save before converting or editing.", style="Muted.TLabel").pack(side="left")
        ttk.Checkbutton(footer, text="Sound effects", variable=self.sound_enabled_var).pack(side="right", padx=(12, 0))
        ttk.Label(footer, textvariable=self.status_var, style="Gold.TLabel").pack(side="right")

    def _build_error_reports_tab(self):
        shell=tk.Frame(self.error_reports_tab,bg=BG,padx=22,pady=18); shell.pack(fill='both',expand=True)
        tk.Label(shell,text='⚠  DIAGNOSTICS & ERROR REPORTS',bg=BG,fg=ACCENT,font=('Georgia',22,'bold')).pack(anchor='w')
        tk.Label(shell,text='Create one TXT file containing the technical information needed to troubleshoot Sacred Zonai Realms.',bg=BG,fg=MUTED,font=('Segoe UI',10),wraplength=1050,justify='left').pack(anchor='w',pady=(5,14))
        card=tk.Frame(shell,bg=CARD,highlightbackground=BORDER,highlightthickness=1,padx=18,pady=16); card.pack(fill='x')
        steps=('1. Leave Sacred Zonai Realms open after the error happens.\n2. Open this Error Reports tab.\n3. Click Save Error Report (.TXT) below.\n4. Save the TXT somewhere easy to find, such as your Desktop.\n5. Write down exactly what you clicked immediately before the error.\n6. Include a screenshot when possible.\n7. Report the issue in KirbyMimi’s Caravan Discord server or to Legendary Savage Gamer on GitHub.\n8. Attach the TXT report and screenshot. Never include passwords or private account information.')
        tk.Label(card,text='How to report a problem',bg=CARD,fg=TEAL,font=('Segoe UI',12,'bold')).pack(anchor='w')
        tk.Label(card,text=steps,bg=CARD,fg=TEXT,font=('Segoe UI',10),justify='left',wraplength=1050).pack(anchor='w',pady=(8,14))
        ttk.Button(card,text='Save Error Report (.TXT)',style='Accent.TButton',command=self.save_error_report).pack(anchor='w')
        self.error_report_status=tk.StringVar(value='No error report saved this session.')
        ttk.Label(card,textvariable=self.error_report_status,style='Muted.TLabel').pack(anchor='w',pady=(10,0))

    def _safe_text_widget(self, widget):
        try: return widget.get('1.0','end-1c')
        except Exception: return ''

    def build_error_report_text(self):
        server_log=self._safe_text_widget(getattr(getattr(self,'server_manager',None),'log',None))
        sav_log=self._safe_text_widget(getattr(self,'to_sav_log',None)); ktml_log=self._safe_text_widget(getattr(self,'to_ktml_log',None))
        sm=getattr(self,'server_manager',None)
        def gv(name,default='—'):
            try: return getattr(sm,name).get()
            except Exception: return default
        lines=['SACRED ZONAI REALMS — ERROR REPORT','='*64,f'Generated: {datetime.now().isoformat(timespec="seconds")}',f'Application: {APP_NAME} {APP_VERSION}',f'OS: {platform.platform()}',f'Python: {platform.python_version()}','', 'HOSTING SETTINGS','-'*64,f'Engine: {gv("host_engine_var")}',f'Primary / selected IP: {gv("primary_ip_var")}',f'Port: {gv("port_var")}',f'Upload bytes/sec: {gv("upload_speed_var")}',f'PvP enabled: {gv("pvp_enabled_var")}',f'Status: {gv("status")}',f'Connection state: {gv("connection_state")}', '', 'KTML → SAV CONVERSION LOG','-'*64,sav_log or 'No conversion log.', '', 'SAV → KTML CONVERSION LOG','-'*64,ktml_log or 'No conversion log.', '', 'LIVE REALM CONSOLE','-'*64,server_log[-30000:] if server_log else 'No realm console output.', '', 'REPORTING INSTRUCTIONS','-'*64,'Attach this TXT to your report in KirbyMimi’s Caravan Discord server or to Legendary Savage Gamer on GitHub.','Explain exactly what you clicked before the error and include a screenshot when possible.','Do not add passwords, tokens, or private account information.']
        return '\n'.join(str(x) for x in lines)

    def save_error_report(self):
        name=f'Sacred_Zonai_Realms_Error_Report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.txt'
        path=filedialog.asksaveasfilename(title='Save Sacred Zonai Realms Error Report',defaultextension='.txt',initialfile=name,filetypes=[('Text report','*.txt')])
        if not path:return
        try:
            Path(path).write_text(self.build_error_report_text(),encoding='utf-8')
            self.error_report_status.set(f'Error report saved: {path}'); self._play_success_sound()
        except Exception as exc: messagebox.showerror(APP_NAME,f'Could not save error report:\n\n{exc}')

    def _credit_card(self, parent, title, subtitle, body, featured=False):
        border = ACCENT if featured else BORDER
        card = tk.Frame(parent, bg=CARD, highlightbackground=border, highlightthickness=2 if featured else 1, padx=18, pady=14)
        card.pack(fill='x', pady=7)
        tk.Label(card, text=title, bg=CARD, fg=ACCENT, font=('Georgia', 15 if featured else 13, 'bold')).pack(anchor='w')
        tk.Label(card, text=subtitle, bg=CARD, fg=TEAL, font=('Segoe UI', 9, 'bold')).pack(anchor='w', pady=(2,7))
        tk.Label(card, text=body, bg=CARD, fg=TEXT, justify='left', anchor='w', wraplength=1030, font=('Segoe UI', 10)).pack(fill='x')
        return card

    def _build_credits_tab(self):
        shell = tk.Frame(self.credits_tab, bg=BG, padx=18, pady=16)
        shell.pack(fill='both', expand=True)

        canvas = tk.Canvas(shell, bg=BG, bd=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(shell, orient='vertical', command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        inner_id = canvas.create_window((0,0), window=inner, anchor='nw')
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(inner_id, width=e.width))

        tk.Label(inner, text='◇  CREDITS & SPECIAL THANKS  ◇', bg=BG, fg=ACCENT, font=('Georgia', 22, 'bold')).pack(anchor='w')
        tk.Label(inner, text='Sacred Zonai Realms exists because of a community of developers, researchers, testers, and friends who made the ideas behind it possible.', bg=BG, fg=MUTED, font=('Segoe UI', 10), wraplength=1040, justify='left').pack(anchor='w', pady=(5,12))

        self._credit_card(
            inner,
            '★ Kirbymimi — Major Project Shout-Out',
            'Creator of the Tears of the Kingdom Multiplayer Project',
            ("The biggest thank-you goes to Kirbymimi. His work, dedication, experimentation, and continued development of Tears of the Kingdom multiplayer are the foundation that made Sacred Zonai Realms' multiplayer and self-hosting tools possible. Without that multiplayer project, this application would not have the same purpose, reach, or community around it. "
             "A major shout-out also goes to the KirbyMimi's Caravan Discord community for testing, troubleshooting, feedback, support, and keeping the multiplayer project moving forward."),
            featured=True,
        )
        self._credit_card(
            inner,
            'Wesley Da Man',
            'Self-hosting inspiration • Future editor / contributor',
            "Special thanks to Wesley Da Man, creator of Zonai Hosting. His supplied source and hosting architecture directly informed Sacred Zonai Realms hosting improvements including stronger realm lifecycle handling, live player-roster reconciliation, safer save replacement, Docker resilience, Romfs merging, and server-save conversion. His work is credited here as a major source and inspiration for Zonai Realms Hosting.",
        )
        self._credit_card(
            inner,
            'Marc Robledo',
            'TOTK save-editor research & reference inspiration',
            "A big thank-you to Marc Robledo for the tremendous work and research behind his Tears of the Kingdom savegame editor. His public editor logic and data were studied as reference material while improving Sacred Zonai Realms' inventory, equipment, variable, coordinate, and completion tools. Credit also belongs to the original researchers and item-data contributors named in those source files, including MacSpazzy, MrCheeze, Karlos007, Echocolat, Exincracci, HylianLZ, ApacheThunder, Phil, savage13, xiyuesaves, and others credited by the original project.",
        )
        self._credit_card(
            inner,
            'Sacred Zonai Realms',
            'Application design, integration & development — ThyHeroOfTime',
            "Sacred Zonai Realms combines save conversion, guarded client-save editing, Docker/native multiplayer hosting, room controls, player-save management, diagnostics, and community-inspired quality-of-life tools into one desktop application. Third-party projects and research remain the work of their respective creators; Sacred Realms does not claim ownership of those projects.",
        )

        legal = tk.Frame(inner, bg=PANEL, highlightbackground=BORDER, highlightthickness=1, padx=14, pady=10)
        legal.pack(fill='x', pady=(8,16))
        tk.Label(legal, text='UNOFFICIAL FAN TOOL', bg=PANEL, fg=TEAL, font=('Segoe UI', 9, 'bold')).pack(anchor='w')
        tk.Label(legal, text='Always back up saves before editing or converting. Sacred Zonai Realms is not affiliated with or endorsed by Nintendo. The Legend of Zelda, Tears of the Kingdom, and related names and assets belong to their respective rights holders. See THIRD_PARTY_NOTICES.txt for additional attribution.', bg=PANEL, fg=MUTED, font=('Segoe UI', 9), wraplength=1040, justify='left').pack(anchor='w', pady=(4,0))

    def _draw_triforce(self, canvas):
        # Three golden triangles, drawn in code so no external image file is needed.
        gold = ACCENT
        outline = "#F3D87C"

        # top
        canvas.create_polygon(
            36, 5, 18, 36, 54, 36,
            fill=gold, outline=outline, width=1,
        )
        # bottom-left
        canvas.create_polygon(
            18, 37, 0, 68, 36, 68,
            fill=gold, outline=outline, width=1,
        )
        # bottom-right
        canvas.create_polygon(
            54, 37, 36, 68, 72, 68,
            fill=gold, outline=outline, width=1,
        )

    def _build_to_sav_tab(self):
        ttk.Label(
            self.to_sav_tab,
            text=(
                "Zonai Direct Converter: convert server .ktml directly into a real progress.sav using the server-authoritative name2hash table. "
                "No clean progress.sav template is required. Always keep an untouched backup."
            ),
            background=PANEL,
            foreground=MUTED,
            wraplength=800,
        ).pack(anchor="w", pady=(0, 12))

        files = ttk.LabelFrame(self.to_sav_tab, text="KTML → SAV Files", padding=14)
        files.pack(fill="x")

        self._file_row(
            files, 0,
            "Server .ktml save",
            self.to_sav_ktml_var,
            self.browse_to_sav_ktml,
        )
        self._file_row(
            files, 1,
            "Legacy template (optional / not used by Zonai Direct)",
            self.to_sav_template_var,
            self.browse_to_sav_template,
        )
        self._file_row(
            files, 2,
            "Converted progress.sav output",
            self.to_sav_output_var,
            self.browse_to_sav_output,
        )

        buttons = ttk.Frame(self.to_sav_tab, style="Panel.TFrame")
        buttons.pack(fill="x", pady=12)

        self.to_sav_button = ttk.Button(
            buttons,
            text="Convert to progress.sav",
            command=self.convert_to_sav,
            style="Accent.TButton",
        )
        self.to_sav_button.pack(side="left")

        ttk.Button(
            buttons,
            text="Validate progress.sav template",
            command=self.validate_sav_template,
        ).pack(side="left", padx=(8, 0))

        ttk.Button(
            buttons,
            text="Open output folder",
            command=lambda: self.open_output_folder(self.to_sav_output_var),
        ).pack(side="left", padx=(8, 0))

        self.to_sav_log = self._make_log(self.to_sav_tab)

    def _build_to_ktml_tab(self):
        ttk.Label(
            self.to_ktml_tab,
            text=(
                "Zonai Direct Converter: convert progress.sav directly back to server KTML using the authoritative server hash table. "
                "A KTML template is no longer required for the normal workflow."
            ),
            background=PANEL,
            foreground=MUTED,
            wraplength=800,
        ).pack(anchor="w", pady=(0, 12))

        files = ttk.LabelFrame(self.to_ktml_tab, text="SAV → KTML Files", padding=14)
        files.pack(fill="x")

        self._file_row(
            files, 0,
            "Your progress.sav",
            self.to_ktml_sav_var,
            self.browse_to_ktml_sav,
        )
        self._file_row(
            files, 1,
            "Legacy KTML template (optional / not used by Zonai Direct)",
            self.to_ktml_template_var,
            self.browse_to_ktml_template,
        )
        self._file_row(
            files, 2,
            "Output server-save.ktml",
            self.to_ktml_output_var,
            self.browse_to_ktml_output,
        )

        ttk.Label(
            self.to_ktml_tab,
            text=(
                "Tip: keep a copy of your current server .ktml before converting so you can restore progress if needed. "
                "This converter is designed around the currently supported 1.2.1 save layout."
            ),
            background=PANEL,
            foreground=MUTED,
            wraplength=800,
        ).pack(anchor="w", pady=(8, 0))

        buttons = ttk.Frame(self.to_ktml_tab, style="Panel.TFrame")
        buttons.pack(fill="x", pady=12)

        self.to_ktml_button = ttk.Button(
            buttons,
            text="Convert to .ktml",
            command=self.convert_to_ktml,
            style="Accent.TButton",
        )
        self.to_ktml_button.pack(side="left")

        ttk.Button(
            buttons,
            text="Validate .ktml template",
            command=self.validate_ktml_template,
        ).pack(side="left", padx=(8, 0))

        ttk.Button(
            buttons,
            text="Open output folder",
            command=lambda: self.open_output_folder(self.to_ktml_output_var),
        ).pack(side="left", padx=(8, 0))

        self.to_ktml_log = self._make_log(self.to_ktml_tab)

    def _file_row(self, parent, row, label, variable, command):
        ttk.Label(
            parent,
            text=label,
            background=PANEL,
            foreground=TEXT,
        ).grid(row=row, column=0, sticky="w", pady=(0, 10))

        ttk.Entry(parent, textvariable=variable).grid(
            row=row,
            column=1,
            sticky="ew",
            padx=8,
            pady=(0, 10),
        )

        ttk.Button(parent, text="Browse…", command=command).grid(
            row=row,
            column=2,
            pady=(0, 10),
        )

        parent.columnconfigure(1, weight=1)

    def _make_log(self, parent):
        frame = ttk.LabelFrame(parent, text="Conversion Report", padding=8)
        frame.pack(fill="both", expand=True)

        log = tk.Text(
            frame,
            wrap="word",
            height=14,
            bg="#0C0E12",
            fg=TEXT,
            insertbackground=TEXT,
            selectbackground="#3B4658",
            selectforeground=TEXT,
            relief="flat",
            borderwidth=0,
            font=("Consolas", 9),
            padx=9,
            pady=9,
        )
        log.pack(fill="both", expand=True)
        log.configure(state="disabled")
        return log

    def _append_log(self, widget, text):
        widget.configure(state="normal")
        widget.insert("end", text + "\n")
        widget.see("end")
        widget.configure(state="disabled")

    def _clear_log(self, widget):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.configure(state="disabled")

    # ---------- KTML -> SAV browse ----------
    def browse_to_sav_ktml(self):
        path = filedialog.askopenfilename(
            title="Select server .ktml",
            filetypes=[("KTML save", "*.ktml"), ("All files", "*.*")],
        )
        if path:
            self.to_sav_ktml_var.set(path)
            if not self.to_sav_output_var.get():
                self.to_sav_output_var.set(
                    str(Path(path).with_name("progress_converted.sav"))
                )

    def browse_to_sav_template(self):
        path = filedialog.askopenfilename(
            title="Select known-good progress.sav",
            filetypes=[("TOTK save", "*.sav"), ("All files", "*.*")],
        )
        if path:
            self.to_sav_template_var.set(path)

    def browse_to_sav_output(self):
        path = filedialog.asksaveasfilename(
            title="Save converted progress.sav",
            defaultextension=".sav",
            initialfile="progress_converted.sav",
            filetypes=[("TOTK save", "*.sav"), ("All files", "*.*")],
        )
        if path:
            self.to_sav_output_var.set(path)

    # ---------- SAV -> KTML browse ----------
    def browse_to_ktml_sav(self):
        path = filedialog.askopenfilename(
            title="Select progress.sav",
            filetypes=[("TOTK save", "*.sav"), ("All files", "*.*")],
        )
        if path:
            self.to_ktml_sav_var.set(path)
            if not self.to_ktml_output_var.get():
                self.to_ktml_output_var.set(
                    str(Path(path).with_name("server-save-converted.ktml"))
                )

    def browse_to_ktml_template(self):
        path = filedialog.askopenfilename(
            title="Select a Zonai .ktml template",
            filetypes=[("KTML save", "*.ktml"), ("All files", "*.*")],
        )
        if path:
            self.to_ktml_template_var.set(path)

    def browse_to_ktml_output(self):
        path = filedialog.asksaveasfilename(
            title="Save converted server .ktml",
            defaultextension=".ktml",
            initialfile="server-save-converted.ktml",
            filetypes=[("KTML save", "*.ktml"), ("All files", "*.*")],
        )
        if path:
            self.to_ktml_output_var.set(path)

    def show_warning(self, message):
        """Play the warning sound and then display a warning popup."""
        self._play_warning_sound()
        messagebox.showwarning(APP_NAME, message)

    # ---------- Validation ----------
    def validate_sav_template(self):
        path = self.to_sav_template_var.get().strip()
        if not path:
            self.show_warning("Choose a progress.sav template first.")
            return

        try:
            data = Path(path).read_bytes()
            marker, hmap = inspect_template(data)
            self._append_log(
                self.to_sav_log,
                "✓ progress.sav template looks valid.\n"
                f"  File: {Path(path).name}\n"
                f"  Size: {len(data):,} bytes\n"
                f"  Hash table marker: 0x{marker:X}\n"
                f"  Hash entries detected: {len(hmap):,}"
            )
            self.status_var.set("progress.sav template valid.")
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))
            self.status_var.set("Template validation failed.")

    def validate_ktml_template(self):
        path = self.to_ktml_template_var.get().strip()
        if not path:
            self.show_warning("Choose a .ktml template first.")
            return

        try:
            root = parse_ktml(Path(path).read_text(encoding="utf-8", errors="strict"))
            count = sum(
                len(v) for v in root.values()
                if isinstance(v, dict)
            )
            self._append_log(
                self.to_ktml_log,
                "✓ KTML template parsed successfully.\n"
                f"  File: {Path(path).name}\n"
                f"  Top-level sections: {len(root):,}\n"
                f"  Named variables/groups: {count:,}"
            )
            self.status_var.set(".ktml template valid.")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"KTML validation failed:\n\n{exc}")
            self.status_var.set("KTML validation failed.")

    # ---------- Conversions ----------
    def convert_to_sav(self):
        ktml = self.to_sav_ktml_var.get().strip()
        output = self.to_sav_output_var.get().strip()
        if not ktml or not output:
            self.show_warning("Choose the server .ktml file and output progress.sav file.")
            return
        ktml_p, output_p = Path(ktml), Path(output)
        if not ktml_p.exists():
            messagebox.showerror(APP_NAME, "The selected KTML file does not exist.")
            return
        self._clear_log(self.to_sav_log); self.status_var.set("Zonai Direct: KTML → progress.sav…")
        self.to_sav_button.configure(state="disabled"); self.update_idletasks()
        try:
            text=ktml_p.read_text(encoding="utf-8", errors="strict")
            data=zonai_ktml_to_sav(text)
            output_p.parent.mkdir(parents=True, exist_ok=True); output_p.write_bytes(data)
            self._append_log(self.to_sav_log, "✓ Zonai Direct conversion finished successfully.")
            self._append_log(self.to_sav_log, "Converter: server-derived exporter + authoritative name2hash.ktml")
            self._append_log(self.to_sav_log, f"Output: {output_p}")
            self._append_log(self.to_sav_log, f"Output size: {len(data):,} bytes")
            self.status_var.set("KTML → SAV conversion complete."); self._play_success_sound()
            messagebox.showinfo(APP_NAME, f"Zonai Direct conversion complete.\n\nCreated:\n{output_p}")
        except Exception as exc:
            self.status_var.set("Conversion failed."); self._play_error_sound()
            self._append_log(self.to_sav_log, "✗ Zonai Direct conversion failed."); self._append_log(self.to_sav_log, str(exc)); self._append_log(self.to_sav_log, traceback.format_exc())
            messagebox.showerror(APP_NAME, f"Conversion failed:\n\n{exc}")
        finally: self.to_sav_button.configure(state="normal")

    def convert_to_ktml(self):
        sav = self.to_ktml_sav_var.get().strip(); output = self.to_ktml_output_var.get().strip()
        if not sav or not output:
            self.show_warning("Choose progress.sav and an output KTML file.")
            return
        sav_p, output_p = Path(sav), Path(output)
        if not sav_p.exists():
            messagebox.showerror(APP_NAME, "The selected progress.sav does not exist."); return
        self._clear_log(self.to_ktml_log); self.status_var.set("Zonai Direct: progress.sav → KTML…")
        self.to_ktml_button.configure(state="disabled"); self.update_idletasks()
        try:
            text=zonai_sav_to_ktml(sav_p.read_bytes())
            output_p.parent.mkdir(parents=True, exist_ok=True)
            with output_p.open("w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            # Parse once with Sacred Zonai Realms' existing parser as a second validation pass.
            parse_ktml(text)
            self._append_log(self.to_ktml_log, "✓ Zonai Direct conversion finished successfully.")
            self._append_log(self.to_ktml_log, "Converter: server-derived binary loader + authoritative name2hash.ktml")
            self._append_log(self.to_ktml_log, f"Output: {output_p}"); self._append_log(self.to_ktml_log, f"Output size: {output_p.stat().st_size:,} bytes")
            self._append_log(self.to_ktml_log, "✓ KTML parsed successfully after conversion.")
            self.status_var.set("SAV → KTML conversion complete."); self._play_success_sound()
            messagebox.showinfo(APP_NAME, f"Zonai Direct conversion complete.\n\nCreated:\n{output_p}")
        except Exception as exc:
            self.status_var.set("Conversion failed."); self._play_error_sound()
            self._append_log(self.to_ktml_log, "✗ Zonai Direct conversion failed."); self._append_log(self.to_ktml_log, str(exc)); self._append_log(self.to_ktml_log, traceback.format_exc())
            messagebox.showerror(APP_NAME, f"Conversion failed:\n\n{exc}")
        finally: self.to_ktml_button.configure(state="normal")

    def open_output_folder(self, variable):
        path = variable.get().strip()
        folder = Path(path).parent if path else Path.cwd()

        if not folder.exists():
            self.show_warning("The output folder does not exist yet.")
            return

        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)
            elif sys.platform == "darwin":
                os.system(f'open "{folder}"')
            else:
                os.system(f'xdg-open "{folder}"')
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))


if __name__ == "__main__":
    app = App()
    app.mainloop()
