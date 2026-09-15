import json
import math
import re
import shutil
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from PIL import Image, ImageTk, ImageEnhance
except Exception:
    Image = ImageTk = ImageEnhance = None

from totk_converter_core import parse_ktml, serialize_ktml, validate_ktml_types, key_hash

CATEGORY_INFO = {
    'weapons': {'label':'Weapons','section':'Pouch.Weapon','valid':'Pouch.Weapon.ValidNum','fields':[
        ('durability','Pouch.Weapon.Content.Life','number','Durability'),
        ('modifier','Pouch.Weapon.Content.Effect.Type','enum','Modifier hash'),
        ('modifierValue','Pouch.Weapon.Content.Effect.Value','number','Modifier value'),
        ('fuseId','Pouch.Weapon.Content.Combined.Name','item','Fused item ID'),
        ('fuseDurability','Pouch.Weapon.Content.Combined.Life','number','Fuse durability'),
        ('extraDurability','Pouch.Weapon.Content.ExtraLife','number','Current fuse bonus'),
        ('recordExtraDurability','Pouch.Weapon.Content.RecordExtraLife','number','Max fuse bonus')]},
    'bows': {'label':'Bows','section':'Pouch.Bow','valid':'Pouch.Bow.ValidNum','fields':[
        ('durability','Pouch.Bow.Content.Life','number','Durability'),
        ('modifier','Pouch.Bow.Content.Effect.Type','enum','Modifier hash'),
        ('modifierValue','Pouch.Bow.Content.Effect.Value','number','Modifier value')]},
    'shields': {'label':'Shields','section':'Pouch.Shield','valid':'Pouch.Shield.ValidNum','fields':[
        ('durability','Pouch.Shield.Content.Life','number','Durability'),
        ('modifier','Pouch.Shield.Content.Effect.Type','enum','Modifier hash'),
        ('modifierValue','Pouch.Shield.Content.Effect.Value','number','Modifier value'),
        ('fuseId','Pouch.Shield.Content.Combined.Name','item','Fused item ID'),
        ('fuseDurability','Pouch.Shield.Content.Combined.Life','number','Fuse durability'),
        ('extraDurability','Pouch.Shield.Content.ExtraLife','number','Current fuse bonus')]},
    'armors': {'label':'Armor','section':'Pouch.Armor','fields':[
        ('dyeColor','Pouch.Armor.Content.ColorVariation','enum','Dye color hash')]},
    'arrows': {'label':'Arrows','section':'Pouch.Arrow','fields':[
        ('quantity','Pouch.Arrow.Content.StockNum','number','Quantity')]},
    'materials': {'label':'Materials','section':'Pouch.Material','fields':[
        ('quantity','Pouch.Material.Content.StockNum','number','Quantity'),
        ('getOrder','Pouch.Material.Content.GetOrder','number','Get order'),
        ('useOrder','Pouch.Material.Content.UseOrder','number','Use order')]},
    'food': {'label':'Food','section':'Pouch.Food','fields':[
        ('quantity','Pouch.Food.Content.StockNum','number','Quantity'),
        ('heartsHeal','Pouch.Food.Content.LifeRecover','number','Heart quarters heal'),
        ('effect','Pouch.Food.Content.Effect.Type','enum','Food effect hash'),
        ('effectMultiplier','Pouch.Food.Content.Effect.Level','number','Effect level'),
        ('effectTime','Pouch.Food.Content.Effect.Time','number','Duration (seconds)'),
        ('price','Pouch.Food.Content.Price','number','Price')], 'recipe':'Pouch.Food.Content.MaterialName'},
    'devices': {'label':'Zonai Devices','section':'Pouch.SpecialParts','fields':[
        ('quantity','Pouch.SpecialParts.Content.StockNum','number','Quantity'),
        ('useOrder','Pouch.SpecialParts.Content.UseOrder','number','Use order')]},
    'key': {'label':'Key Items','section':'Pouch.KeyItem','fields':[
        ('quantity','Pouch.KeyItem.Content.StockNum','number','Quantity')]},
    'abilities': {'label':'Abilities','section':'Pouch.SpecialPower','fields':[]},
}

STATUS_FIELDS = [
    ('Rupees','PlayerStatus.CurrentRupee','Int'),
    ('Current Hearts (quarter-hearts)','PlayerStatus.Life','Int'),
    ('Maximum Hearts (quarter-hearts)','PlayerStatus.MaxLife','Int'),
    ('Maximum Stamina','PlayerStatus.MaxStamina','Float'),
    ('Maximum Zonai Energy','PlayerStatus.MaxEnergy','Float'),
    ('Extra Stamina','PlayerStatus.ExtraStamina','Float'),
    ('Extra Zonai Energy','PlayerStatus.ExtraEnergy','Float'),
    ('Poes / Mamo','PlayerStatus.CurrentMamo','Int'),
]

SAGE_FIELDS = [
    ('Sidon / Water Sage', 'PlayerStatus.Companion.Water.IsSummon'),
    ('Tulin / Wind Sage', 'PlayerStatus.Companion.Wind.IsSummon'),
    ('Yunobo / Fire Sage', 'PlayerStatus.Companion.Fire.IsSummon'),
    ('Riju / Lightning Sage', 'PlayerStatus.Companion.Electric.IsSummon'),
    ('Mineru / Spirit Sage', 'PlayerStatus.Companion.Soul.IsSummon'),
]

MODIFIER_NAMES = ['None','AttackUp','AttackUpPlus','DurabilityUp','DurabilityUpPlus','FinishBlow','LongThrow','RapidFire','FiveWay','GuardUp','GuardUpPlus']


def _resource_path(name):
    import sys
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    return base / name


def load_json_resource(name, fallback):
    p = _resource_path(name)
    try:
        if p.exists():
            return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        pass
    return fallback


def load_catalog():
    return load_json_resource('totk_catalog.json', {'categories':{},'enumHashes':{},'durability':{},'fuseDurability':{}})


def load_reference():
    return load_json_resource('marc_reference.json', {
        'itemMaximumQuantity':{}, 'keyCountable':[], 'abilities':[],
        'defaultDurability':{}, 'fuseDurability':{}, 'completionGroups':{},
        'mapLayers':{'skyMinExclusive':750,'depthsMaxExclusive':-300}
    })

def load_coordinate_map():
    return load_json_resource('totk_coordinate_map.json', {
        'coordinateSystem':{'horizontalAxes':['x','z'],'heightAxis':'y','xMin':-5000,'xMax':5000,'zMin':-4200,'zMax':4200},
        'layers':{'surface':[],'sky':[],'depths':[]}, 'presets':[]
    })


class ClientSaveEditorFrame(ttk.Frame):
    """Sacred Realms KTML editor with guarded, reference-driven helper tools."""

    def __init__(self, parent, bg='#07110F', muted='#9BAAA4', accent='#D8B85B'):
        super().__init__(parent, padding=12)
        self.bg, self.muted, self.accent = bg, muted, accent
        self.doc = None
        self.path = None
        self.catalog = load_catalog()
        self.reference = load_reference()
        self.coordinate_map = load_coordinate_map()
        self.catalog_names = {
            cat:{x.get('id',''):x.get('name',x.get('id','')) for x in items}
            for cat,items in self.catalog.get('categories',{}).items()
        }
        self.catalog_ids_by_name = {
            cat:{x.get('name',x.get('id','')).lower():x.get('id','') for x in items}
            for cat,items in self.catalog.get('categories',{}).items()
        }
        self.reverse_enums = {str(v):k for k,v in self.catalog.get('enumHashes',{}).items()}
        self.dirty = False
        self._hash_cache = None
        self._build()

    # ---------- UI ----------
    def _build(self):
        top=ttk.Frame(self); top.pack(fill='x', pady=(0,8))
        ttk.Label(top,text='Client Save Editor',style='SectionTitle.TLabel').pack(side='left')
        ttk.Button(top,text='Open Save…',command=self.open_file).pack(side='left',padx=(12,4))
        ttk.Button(top,text='Load Clean Default',command=self.load_default).pack(side='left',padx=4)
        ttk.Button(top,text='Save Edited Copy…',command=self.save_as,style='Accent.TButton').pack(side='left',padx=4)
        ttk.Button(top,text='Create Backup',command=self.backup_current).pack(side='left',padx=4)
        self.file_label=ttk.Label(top,text='No save loaded',style='Gold.TLabel'); self.file_label.pack(side='right')

        note=ttk.Label(
            self,
            text='Safe workflow: open KTML → edit → validate → save a new copy. Reference-driven tools are guarded and never overwrite your original automatically.',
            style='Muted.TLabel'
        )
        note.pack(anchor='w',pady=(0,8))

        self.editor_glint=tk.Canvas(self,height=4,bg=self.bg,highlightthickness=0,bd=0)
        self.editor_glint.pack(fill='x',pady=(0,3))
        self.nb=ttk.Notebook(self); self.nb.pack(fill='both',expand=True)
        self.status_tab=ttk.Frame(self.nb,padding=10)
        self.inv_tab=ttk.Frame(self.nb,padding=10)
        self.comp_tab=ttk.Frame(self.nb,padding=10)
        self.auto_tab=ttk.Frame(self.nb,padding=10)
        self.adv_tab=ttk.Frame(self.nb,padding=10)
        self.nb.add(self.status_tab,text='△ Player Status')
        self.nb.add(self.inv_tab,text='◇ Inventory & Equipment')
        self.nb.add(self.comp_tab,text='✦ Completion Dashboard')
        self.nb.add(self.auto_tab,text='⚙ Autobuilder Inspector')
        self.nb.add(self.adv_tab,text='⌘ Advanced Variables')
        self.nb.bind('<<NotebookTabChanged>>',self._on_editor_tab_changed)
        self._build_status(); self._build_inventory(); self._build_completion(); self._build_autobuilder(); self._build_advanced()

    def _build_status(self):
        wrapper=ttk.Frame(self.status_tab); wrapper.pack(fill='both',expand=True)
        left=ttk.Frame(wrapper); left.pack(side='left',fill='both',expand=True,padx=(0,8))
        right=ttk.Frame(wrapper); right.pack(side='left',fill='both',expand=True,padx=(8,0))

        self.status_vars={}
        lf=ttk.LabelFrame(left,text='Core Player Stats',padding=12); lf.pack(fill='x')
        for i,(label,path,typ) in enumerate(STATUS_FIELDS):
            ttk.Label(lf,text=label).grid(row=i,column=0,sticky='w',pady=3)
            v=tk.StringVar(); self.status_vars[path]=(v,typ)
            ttk.Entry(lf,textvariable=v,width=24).grid(row=i,column=1,sticky='ew',padx=8,pady=3)
        lf.columnconfigure(1,weight=1)
        ttk.Button(lf,text='Apply Stats',command=self.apply_status,style='Accent.TButton').grid(row=len(STATUS_FIELDS),column=0,columnspan=2,sticky='w',pady=(8,0))

        sf=ttk.LabelFrame(right,text='Sage Summon Status',padding=12); sf.pack(fill='x')
        self.sage_vars={}
        for i,(label,path) in enumerate(SAGE_FIELDS):
            v=tk.BooleanVar(); self.sage_vars[path]=v
            ttk.Checkbutton(sf,text=label,variable=v,command=lambda p=path:self._apply_bool(p)).grid(row=i,column=0,sticky='w',pady=2)

        rf=ttk.LabelFrame(right,text='World Position is a main application tab',padding=12); rf.pack(fill='x',pady=10)
        ttk.Label(rf,text='Use the top-level ⌖ World Position Map tab between Zonai Realms Hosting and Credits. It shares this same loaded KTML and now uses your actual Sky / Surface / Depths map artwork.',style='Muted.TLabel',wraplength=430).pack(anchor='w')

    def attach_world_position_tab(self, parent):
        """Mount the world-position editor into the application's dedicated top-level tab."""
        self.world_tab = parent
        self._build_world_position()
        if self.doc:
            self.refresh_all()

    def _build_world_position(self):
        """Build the Hylian cartography screen using the user's supplied Sky/Surface/Depths maps."""
        self.map_layer_choice=tk.StringVar(value='Surface')
        self.pos_vars={axis:tk.StringVar() for axis in ('x','y','z')}
        self.map_layer_var=tk.StringVar(value='Map Layer: Surface / Hyrule')
        self.y_mode_var=tk.StringVar(value='Nearest reference altitude (recommended)')
        self.map_status_var=tk.StringVar(value='Drag the gold reticle across the map. X/Z follow the cursor; Y is estimated from nearby reference coordinates.')
        self.preset_var=tk.StringVar()
        self._map_dragging=False
        self._map_reticle_xy=None
        self._map_points_visible=[]
        self._world_map_photo=None
        self._world_thumb_photos={}
        self._layer_cards={}

        # Outer frame deliberately uses direct Tk widgets so the map editor can match the
        # Hylian mock-up more closely than a stock ttk notebook page.
        root=tk.Frame(self.world_tab,bg='#061412')
        root.pack(fill='both',expand=True)

        # Dedicated top-level map toolbar. This uses the exact same loaded KTML as
        # Client Save Editor, so position recovery never edits a disconnected copy.
        map_toolbar=tk.Frame(root,bg='#071714',highlightbackground='#294139',highlightthickness=1,padx=8,pady=7)
        map_toolbar.pack(fill='x',padx=6,pady=(6,2))
        tk.Label(map_toolbar,text='⌖  WORLD POSITION MAP',bg='#071714',fg='#F4F1E8',font=('Georgia',13,'bold')).pack(side='left')
        tk.Button(map_toolbar,text='Open Client Save…',command=self.open_file,bg='#13352E',fg='#F4F1E8',activebackground='#245348',activeforeground='#FFFFFF',relief='flat',font=('Segoe UI',9,'bold'),cursor='hand2',padx=10,pady=5).pack(side='left',padx=(14,4))
        tk.Button(map_toolbar,text='Load Clean Default',command=self.load_default,bg='#102522',fg='#F4F1E8',activebackground='#1D4037',activeforeground='#FFFFFF',relief='flat',font=('Segoe UI',9,'bold'),cursor='hand2',padx=10,pady=5).pack(side='left',padx=4)
        tk.Button(map_toolbar,text='Save Edited Copy…',command=self.save_as,bg='#173C34',fg='#F6E6A8',activebackground='#245348',activeforeground='#FFF4C5',relief='flat',highlightbackground='#D8B85B',highlightthickness=1,font=('Georgia',9,'bold'),cursor='hand2',padx=10,pady=5).pack(side='left',padx=4)
        self.world_file_var=tk.StringVar(value='No client save loaded')
        tk.Label(map_toolbar,textvariable=self.world_file_var,bg='#071714',fg='#86CFC0',font=('Segoe UI',8,'bold')).pack(side='right')

        # Layer cards ---------------------------------------------------------
        layer_strip=tk.Frame(root,bg='#061412',pady=8)
        layer_strip.pack(fill='x',padx=6)
        layer_strip.grid_columnconfigure((0,1,2),weight=1,uniform='layers')
        layer_specs=[
            ('Sky','Skies of Hyrule','sky.png'),
            ('Surface','Surface / Hyrule','surface.png'),
            ('Depths','Depths of Hyrule','depths.png'),
        ]
        for col,(value,title,img) in enumerate(layer_specs):
            card=tk.Frame(layer_strip,bg='#102522',highlightbackground='#315047',highlightthickness=1,cursor='hand2')
            card.grid(row=0,column=col,sticky='ew',padx=5,ipady=2)
            thumb=tk.Canvas(card,width=86,height=52,bg='#0A1816',highlightthickness=0,bd=0,cursor='hand2')
            thumb.pack(side='left',padx=(8,7),pady=6)
            self._draw_layer_thumbnail(thumb,img,value)
            text=tk.Frame(card,bg='#102522',cursor='hand2'); text.pack(side='left',fill='both',expand=True,pady=7)
            title_lbl=tk.Label(text,text=title,bg='#102522',fg='#F4F1E8',font=('Georgia',13,'bold'),anchor='w',cursor='hand2')
            title_lbl.pack(fill='x')
            sub={'Sky':'SKY REALM','Surface':'THE LAND OF HYRULE','Depths':'THE WORLD BELOW'}[value]
            sub_lbl=tk.Label(text,text=sub,bg='#102522',fg='#86CFC0',font=('Segoe UI',8,'bold'),anchor='w',cursor='hand2')
            sub_lbl.pack(fill='x',pady=(2,0))
            self._layer_cards[value]=(card,title_lbl,sub_lbl)
            for wdg in (card,thumb,text,title_lbl,sub_lbl):
                wdg.bind('<Button-1>',lambda e,v=value:self._select_map_layer(v))
        self._refresh_layer_cards()

        # Main map and right inspector ---------------------------------------
        body=tk.Frame(root,bg='#061412')
        body.pack(fill='both',expand=True,padx=6,pady=(0,6))
        body.grid_rowconfigure(0,weight=1); body.grid_columnconfigure(0,weight=5); body.grid_columnconfigure(1,weight=2)

        map_shell=tk.Frame(body,bg='#071714',highlightbackground='#D8B85B',highlightthickness=1)
        map_shell.grid(row=0,column=0,sticky='nsew',padx=(0,8))
        map_shell.grid_rowconfigure(1,weight=1); map_shell.grid_columnconfigure(0,weight=1)
        self.world_map_title=tk.Label(map_shell,text='SURFACE / HYRULE',bg='#071714',fg='#F4F1E8',font=('Georgia',15,'bold'),anchor='w',padx=14,pady=8)
        self.world_map_title.grid(row=0,column=0,sticky='ew')
        self.world_canvas=tk.Canvas(map_shell,bg='#081713',highlightthickness=0,bd=0,cursor='crosshair')
        self.world_canvas.grid(row=1,column=0,sticky='nsew',padx=2,pady=(0,2))
        self.world_canvas.bind('<Configure>',lambda e:self._draw_world_map())
        self.world_canvas.bind('<Button-1>',self._map_press)
        self.world_canvas.bind('<B1-Motion>',self._map_drag)
        self.world_canvas.bind('<ButtonRelease-1>',self._map_release)

        map_legend=tk.Label(map_shell,text='✦ Gold reticle = Link   •   drag anywhere to update X / Y / Z   •   map artwork supplied by ThyHeroOfTime',bg='#071714',fg='#9BAAA4',font=('Segoe UI',8),anchor='w',padx=12,pady=5)
        map_legend.grid(row=2,column=0,sticky='ew')

        side=tk.Frame(body,bg='#071714',highlightbackground='#294139',highlightthickness=1,padx=10,pady=10)
        side.grid(row=0,column=1,sticky='nsew')
        side.grid_columnconfigure(0,weight=1)

        tk.Label(side,text='⌖  WORLD POSITION',bg='#071714',fg='#F4F1E8',font=('Georgia',16,'bold'),anchor='w').grid(row=0,column=0,sticky='ew',pady=(0,8))
        tk.Label(side,text='View and edit Link’s position across all three realms.',bg='#071714',fg='#9BAAA4',font=('Segoe UI',9),anchor='w').grid(row=1,column=0,sticky='ew',pady=(0,10))

        coord=tk.Frame(side,bg='#0D1B18',highlightbackground='#315047',highlightthickness=1,padx=10,pady=10)
        coord.grid(row=2,column=0,sticky='ew'); coord.grid_columnconfigure(1,weight=1)
        tk.Label(coord,text='CURRENT POSITION',bg='#0D1B18',fg='#D8B85B',font=('Georgia',11,'bold')).grid(row=0,column=0,columnspan=2,sticky='w',pady=(0,6))
        for row,axis in enumerate(('x','y','z'),start=1):
            tk.Label(coord,text=axis.upper()+':',bg='#0D1B18',fg='#F4F1E8',font=('Georgia',11,'bold')).grid(row=row,column=0,sticky='w',pady=4)
            ent=tk.Entry(coord,textvariable=self.pos_vars[axis],bg='#081512',fg='#F4F1E8',insertbackground='#F4F1E8',relief='flat',highlightbackground='#315047',highlightcolor='#D8B85B',highlightthickness=1,font=('Consolas',11))
            ent.grid(row=row,column=1,sticky='ew',padx=(8,0),pady=4,ipady=5)
            ent.bind('<KeyRelease>',lambda e:self._manual_position_changed())
        tk.Label(coord,textvariable=self.map_layer_var,bg='#0D1B18',fg='#86CFC0',font=('Segoe UI',9,'bold'),anchor='w').grid(row=4,column=0,columnspan=2,sticky='ew',pady=(7,2))
        tk.Label(coord,text='Y while dragging',bg='#0D1B18',fg='#9BAAA4',font=('Segoe UI',8)).grid(row=5,column=0,sticky='w',pady=(6,2))
        ymode=ttk.Combobox(coord,textvariable=self.y_mode_var,state='readonly',values=['Nearest reference altitude (recommended)','Keep current Y','Layer safe default'],width=28)
        ymode.grid(row=5,column=1,sticky='ew',padx=(8,0),pady=(6,2))
        apply_btn=tk.Button(coord,text='△  Apply Link Position to Save',command=self.apply_position,bg='#173C34',fg='#F6E6A8',activebackground='#245348',activeforeground='#FFF4C5',relief='flat',highlightbackground='#D8B85B',highlightthickness=1,font=('Georgia',10,'bold'),cursor='hand2',pady=9)
        apply_btn.grid(row=6,column=0,columnspan=2,sticky='ew',pady=(10,0))

        preset=tk.Frame(side,bg='#0D1B18',highlightbackground='#315047',highlightthickness=1,padx=10,pady=10)
        preset.grid(row=3,column=0,sticky='ew',pady=9); preset.grid_columnconfigure(0,weight=1)
        tk.Label(preset,text='✦  RESCUE PRESETS',bg='#0D1B18',fg='#D8B85B',font=('Georgia',11,'bold')).grid(row=0,column=0,sticky='w')
        presets=[x.get('label','') for x in self.coordinate_map.get('presets',[]) if x.get('label')]
        pc=ttk.Combobox(preset,textvariable=self.preset_var,state='readonly',values=presets,width=30)
        pc.grid(row=1,column=0,sticky='ew',pady=(7,6))
        if presets: pc.current(0)
        tk.Button(preset,text='⌖  Load Rescue Preset',command=self._load_rescue_preset,bg='#13352E',fg='#F4F1E8',activebackground='#245348',activeforeground='#FFFFFF',relief='flat',highlightbackground='#D8B85B',highlightthickness=1,font=('Segoe UI',9,'bold'),cursor='hand2',pady=7).grid(row=2,column=0,sticky='ew')
        tk.Button(preset,text='Snap to Nearest Safe Reference',command=self._snap_to_nearest_reference,bg='#102522',fg='#F4F1E8',activebackground='#1D4037',activeforeground='#FFFFFF',relief='flat',font=('Segoe UI',9,'bold'),cursor='hand2',pady=6).grid(row=3,column=0,sticky='ew',pady=(6,0))

        recovery=tk.Frame(side,bg='#0D1B18',highlightbackground='#D8B85B',highlightthickness=1,padx=10,pady=10)
        recovery.grid(row=4,column=0,sticky='ew',pady=(0,9)); recovery.grid_columnconfigure(0,weight=1)
        tk.Label(recovery,text='⚠  MULTIPLAYER SPAWN RECOVERY',bg='#0D1B18',fg='#F6E6A8',font=('Georgia',10,'bold'),anchor='w').grid(row=0,column=0,sticky='ew')
        tk.Label(recovery,text='For the affected save that enters DmF_SY_FallDownReturn repeatedly after Actor Sync/MainField. This resets only verified spawn/fall context and moves Link to Lookout Landing.',bg='#0D1B18',fg='#F4F1E8',font=('Segoe UI',8),justify='left',anchor='w',wraplength=300).grid(row=1,column=0,sticky='ew',pady=(5,7))
        tk.Button(recovery,text='🛟  Build Safe Spawn Recovery',command=self.apply_multiplayer_spawn_recovery,bg='#5A3C19',fg='#FFF1B8',activebackground='#765127',activeforeground='#FFFFFF',relief='flat',highlightbackground='#E7CC78',highlightthickness=1,font=('Georgia',9,'bold'),cursor='hand2',pady=7).grid(row=2,column=0,sticky='ew')

        notes=tk.Frame(side,bg='#0D1B18',highlightbackground='#315047',highlightthickness=1,padx=10,pady=10)
        notes.grid(row=5,column=0,sticky='nsew'); side.grid_rowconfigure(5,weight=1)
        tk.Label(notes,text='MAP / RECOVERY STATUS',bg='#0D1B18',fg='#D8B85B',font=('Georgia',10,'bold'),anchor='w').pack(fill='x')
        tk.Label(notes,textvariable=self.map_status_var,bg='#0D1B18',fg='#F4F1E8',font=('Segoe UI',9),justify='left',anchor='nw',wraplength=310).pack(fill='x',pady=(6,7))
        tk.Label(notes,text='The supplied images are visual map layers. Sacred Realms keeps its X/Z coordinate transform on top of them, while Y is calculated from the selected altitude mode.',bg='#0D1B18',fg='#9BAAA4',font=('Segoe UI',8),justify='left',anchor='nw',wraplength=310).pack(fill='x')

    def _draw_layer_thumbnail(self, canvas, filename, layer_value):
        """Draw a tiny preview of one of the user's map images."""
        try:
            if Image and ImageTk:
                path=_resource_path('assets/maps/'+filename)
                if path.exists():
                    im=Image.open(path).convert('RGB')
                    im.thumbnail((86,52),Image.Resampling.LANCZOS)
                    photo=ImageTk.PhotoImage(im)
                    self._world_thumb_photos[layer_value]=photo
                    canvas.create_image(43,26,image=photo)
                    return
        except Exception:
            pass
        canvas.create_text(43,26,text={'Sky':'☁','Surface':'△','Depths':'◈'}.get(layer_value,'◇'),fill='#D8B85B',font=('Georgia',21,'bold'))

    def _select_map_layer(self, value):
        self.map_layer_choice.set(value)
        self._world_layer_changed()
        self._refresh_layer_cards()

    def _refresh_layer_cards(self):
        selected=self.map_layer_choice.get()
        for value,widgets in getattr(self,'_layer_cards',{}).items():
            card,title,sub=widgets
            active=value==selected
            card.configure(bg='#15372F' if active else '#102522',highlightbackground='#E7CC78' if active else '#315047',highlightthickness=2 if active else 1)
            title.configure(bg=card['bg'],fg='#FFF1B8' if active else '#F4F1E8')
            sub.configure(bg=card['bg'],fg='#86CFC0')
        if hasattr(self,'world_map_title'):
            self.world_map_title.configure(text={'Sky':'SKIES OF HYRULE','Surface':'SURFACE / HYRULE','Depths':'DEPTHS OF HYRULE'}.get(selected,selected.upper()))

    def _world_bounds(self):
        cs=self.coordinate_map.get('coordinateSystem',{})
        return (float(cs.get('xMin',-5000)),float(cs.get('xMax',5000)),float(cs.get('zMin',-4200)),float(cs.get('zMax',4200)))

    def _world_to_canvas(self,x,z):
        c=self.world_canvas; w=max(2,c.winfo_width()); h=max(2,c.winfo_height()); pad=24
        xmin,xmax,zmin,zmax=self._world_bounds()
        px=pad+(float(x)-xmin)/(xmax-xmin)*max(1,w-2*pad)
        py=pad+(zmax-float(z))/(zmax-zmin)*max(1,h-2*pad)
        return px,py

    def _canvas_to_world(self,px,py):
        c=self.world_canvas; w=max(2,c.winfo_width()); h=max(2,c.winfo_height()); pad=24
        xmin,xmax,zmin,zmax=self._world_bounds()
        px=min(max(px,pad),max(pad,w-pad)); py=min(max(py,pad),max(pad,h-pad))
        x=xmin+(px-pad)/max(1,w-2*pad)*(xmax-xmin)
        z=zmax-(py-pad)/max(1,h-2*pad)*(zmax-zmin)
        return x,z

    def _layer_key(self):
        return {'Sky':'sky','Surface':'surface','Depths':'depths'}.get(self.map_layer_choice.get(),'surface')

    def _layer_from_height(self,y):
        try: y=float(y)
        except Exception: return 'Surface'
        if y>750: return 'Sky'
        if y<-300: return 'Depths'
        return 'Surface'

    def _reference_points(self,layer=None):
        return self.coordinate_map.get('layers',{}).get(layer or self._layer_key(),[])

    def _nearest_reference(self,x,z,layer=None):
        pts=self._reference_points(layer)
        if not pts: return None
        return min(pts,key=lambda p:(float(p.get('x',0))-x)**2+(float(p.get('z',0))-z)**2)

    def _estimate_y(self,x,z):
        mode=self.y_mode_var.get()
        if mode=='Keep current Y':
            try: return float(self.pos_vars['y'].get())
            except Exception: pass
        if mode=='Layer safe default':
            return {'sky':1600.0,'surface':150.0,'depths':-420.0}[self._layer_key()]
        pts=self._reference_points()
        if not pts: return {'sky':1600.0,'surface':150.0,'depths':-420.0}[self._layer_key()]
        nearest=sorted(pts,key=lambda p:(float(p.get('x',0))-x)**2+(float(p.get('z',0))-z)**2)[:4]
        weighted=[]
        for p in nearest:
            d=math.hypot(float(p.get('x',0))-x,float(p.get('z',0))-z)
            weighted.append((1.0/max(50.0,d),float(p.get('y',0))))
        y=sum(w*v for w,v in weighted)/sum(w for w,v in weighted)
        return y+5.0

    def _draw_world_map(self):
        """Render the selected supplied map image, then overlay coordinate grid and Link reticle."""
        if not hasattr(self,'world_canvas') or not self.world_canvas.winfo_exists(): return
        c=self.world_canvas; c.delete('all'); w=max(2,c.winfo_width()); h=max(2,c.winfo_height())
        layer=self._layer_key()
        bg={'surface':'#0A1511','sky':'#0A1116','depths':'#050B0C'}[layer]
        c.configure(bg=bg)

        # Real map artwork supplied by the user.  We intentionally keep an inset around
        # the image so the coordinate overlay has a stable mathematical rectangle.
        pad=18
        image_names={'surface':'surface.png','sky':'sky.png','depths':'depths.png'}
        try:
            if Image and ImageTk:
                path=_resource_path('assets/maps/'+image_names[layer])
                if path.exists():
                    im=Image.open(path).convert('RGB')
                    # Remove large title/black margins while retaining the map itself.
                    if layer=='sky':
                        box=(55,105,1495,1115)
                    elif layer=='depths':
                        box=(20,165,1515,1125)
                    else:
                        box=(90,45,1860,1615)
                    box=(max(0,box[0]),max(0,box[1]),min(im.width,box[2]),min(im.height,box[3]))
                    im=im.crop(box)
                    # Slightly darken so UI overlays and reticle remain readable.
                    if ImageEnhance:
                        im=ImageEnhance.Brightness(im).enhance(0.82)
                    im=im.resize((max(1,w-2*pad),max(1,h-2*pad)),Image.Resampling.LANCZOS)
                    self._world_map_photo=ImageTk.PhotoImage(im)
                    c.create_image(pad,pad,image=self._world_map_photo,anchor='nw',tags='map_image')
        except Exception as exc:
            self.map_status_var.set(f'Map artwork could not be displayed ({exc}). Coordinate editing still works.')

        # Fine cartography grid over the supplied image.  This is intentionally subtle.
        grid='#24443F' if layer!='depths' else '#204048'
        for i in range(1,10):
            x=pad+i*(max(1,w-2*pad)/10); c.create_line(x,pad,x,h-pad,fill=grid,width=1,stipple='gray50')
        for i in range(1,8):
            y=pad+i*(max(1,h-2*pad)/8); c.create_line(pad,y,w-pad,y,fill=grid,width=1,stipple='gray50')

        # Coordinate labels provide a quick visual relationship between the image and save data.
        xmin,xmax,zmin,zmax=self._world_bounds()
        for i in range(0,11,2):
            xval=xmin+(xmax-xmin)*(i/10); px=pad+(w-2*pad)*(i/10)
            c.create_text(px,pad+7,text=f'{xval:.0f}',fill='#D9C988',font=('Segoe UI',7),anchor='n')
        for i in range(0,9,2):
            zval=zmax-(zmax-zmin)*(i/8); py=pad+(h-2*pad)*(i/8)
            c.create_text(pad+4,py,text=f'{zval:.0f}',fill='#D9C988',font=('Segoe UI',7),anchor='w')

        # Known-reference overlay remains available, but is intentionally tiny because the
        # supplied maps already contain their own rich landmark markings.
        pts=self._reference_points(); self._map_points_visible=pts
        sample=0
        for p in pts:
            kind=p.get('kind','location')
            if kind=='location':
                sample+=1
                if sample%8: continue
            px,py=self._world_to_canvas(p.get('x',0),p.get('z',0))
            if kind=='tower':
                c.create_polygon(px,py-4,px+4,py,px,py+4,px-4,py,fill='#FFD768',outline='',tags='ref')
            elif kind=='lightroot' and layer=='depths':
                c.create_oval(px-2,py-2,px+2,py+2,fill='#74E1CB',outline='',tags='ref')
            elif kind=='shrine' and layer!='depths':
                c.create_oval(px-1.7,py-1.7,px+1.7,py+1.7,fill='#FFF0A8',outline='',tags='ref')

        # Small realm label in the same spirit as the visual mock-up.
        realm={'surface':'HYRULE • SURFACE','sky':'SKIES OF HYRULE','depths':'DEPTHS OF HYRULE'}[layer]
        c.create_rectangle(pad+8,pad+8,pad+205,pad+37,fill='#07110F',outline='#D8B85B',width=1)
        c.create_text(pad+18,pad+22,text=realm,anchor='w',fill='#F4F1E8',font=('Georgia',10,'bold'))

        try:
            x=float(self.pos_vars['x'].get()); z=float(self.pos_vars['z'].get()); self._draw_reticle(*self._world_to_canvas(x,z))
        except Exception: pass

    def _draw_reticle(self,px,py):
        c=self.world_canvas; c.delete('link_reticle'); r=11
        c.create_oval(px-r,py-r,px+r,py+r,outline='#FFD768',width=2,tags='link_reticle')
        c.create_line(px-r-7,py,px+r+7,py,fill='#FFD768',width=1,tags='link_reticle')
        c.create_line(px,py-r-7,px,py+r+7,fill='#FFD768',width=1,tags='link_reticle')
        c.create_oval(px-3,py-3,px+3,py+3,fill='#FFF0A8',outline='',tags='link_reticle')
        self._map_reticle_xy=(px,py)

    def _set_position_from_canvas(self,px,py):
        x,z=self._canvas_to_world(px,py); y=self._estimate_y(x,z)
        self.pos_vars['x'].set(f'{x:.3f}'); self.pos_vars['y'].set(f'{y:.3f}'); self.pos_vars['z'].set(f'{z:.3f}')
        self._draw_reticle(*self._world_to_canvas(x,z)); self._refresh_map_layer()
        near=self._nearest_reference(x,z)
        label=(near or {}).get('label') or (near or {}).get('kind','reference point')
        if near:
            dist=math.hypot(float(near.get('x',0))-x,float(near.get('z',0))-z)
            self.map_status_var.set(f'Reticle: X {x:.1f} • Y {y:.1f} • Z {z:.1f} • nearest {label} ≈ {dist:.0f} units away')

    def _map_press(self,event): self._map_dragging=True; self._set_position_from_canvas(event.x,event.y)
    def _map_drag(self,event):
        if self._map_dragging: self._set_position_from_canvas(event.x,event.y)
    def _map_release(self,event): self._map_dragging=False; self._set_position_from_canvas(event.x,event.y)

    def _world_layer_changed(self):
        key=self._layer_key(); current=self._layer_from_height(self.pos_vars['y'].get())
        if current!=self.map_layer_choice.get():
            self.pos_vars['y'].set(str({'sky':1600.0,'surface':150.0,'depths':-420.0}[key]))
        self._refresh_map_layer(); self._refresh_layer_cards(); self._draw_world_map()

    def _manual_position_changed(self):
        self._refresh_map_layer(); self._draw_world_map()

    def _center_map_on_saved(self):
        pos=self._find('World_PlayerPos',None) or self._find('PlayerStatus.SavePos',None)
        if isinstance(pos,dict):
            for a in ('x','y','z'): self.pos_vars[a].set(str(pos.get(a,'')))
            self.map_layer_choice.set(self._layer_from_height(pos.get('y',0))); self._refresh_map_layer(); self._draw_world_map()

    def _snap_to_nearest_reference(self):
        try: x=float(self.pos_vars['x'].get()); z=float(self.pos_vars['z'].get())
        except Exception: return
        p=self._nearest_reference(x,z)
        if not p: return
        self.pos_vars['x'].set(f"{float(p['x']):.4f}"); self.pos_vars['y'].set(f"{float(p['y'])+5.0:.4f}"); self.pos_vars['z'].set(f"{float(p['z']):.4f}")
        label=p.get('label') or p.get('kind','reference point')
        self.map_status_var.set(f'Snapped to {label} with +5 vertical clearance. Save an edited copy before testing.')
        self._refresh_map_layer(); self._draw_world_map()

    def _load_rescue_preset(self):
        label=self.preset_var.get(); p=next((x for x in self.coordinate_map.get('presets',[]) if x.get('label')==label),None)
        if not p: return
        for a in ('x','y','z'): self.pos_vars[a].set(str(p[a]))
        self.map_layer_choice.set({'surface':'Surface','sky':'Sky','depths':'Depths'}.get(p.get('layer'),'Surface'))
        self.map_status_var.set(f"Loaded rescue preset: {label}. Click Apply Link Position to Save, then Save Edited Copy.")
        self._refresh_map_layer(); self._draw_world_map()

    def _build_inventory(self):
        controls=ttk.Frame(self.inv_tab); controls.pack(fill='x',pady=(0,8))
        ttk.Label(controls,text='Category:').pack(side='left')
        self.category_var=tk.StringVar(value='materials')
        labels=[f"{k} — {v['label']}" for k,v in CATEGORY_INFO.items()]
        cb=ttk.Combobox(controls,state='readonly',width=23,values=labels)
        cb.set(labels[list(CATEGORY_INFO).index('materials')])
        cb.pack(side='left',padx=6)
        def category_changed(_=None):
            raw=cb.get().split(' — ',1)[0]
            self.category_var.set(raw); self.refresh_inventory()
        cb.bind('<<ComboboxSelected>>',category_changed)

        ttk.Label(controls,text='Find item:').pack(side='left',padx=(12,0))
        self.item_var=tk.StringVar(); self.item_combo=ttk.Combobox(controls,textvariable=self.item_var,width=35); self.item_combo.pack(side='left',padx=6,fill='x',expand=True)
        ttk.Button(controls,text='Add / Replace',command=self.put_item).pack(side='left',padx=4)
        ttk.Button(controls,text='Clear Slot',command=self.clear_slot).pack(side='left',padx=4)

        tools=ttk.LabelFrame(self.inv_tab,text='Safe Quick Tools — based on referenced TOTK editor behavior',padding=8); tools.pack(fill='x',pady=(0,8))
        ttk.Button(tools,text='Max Selected Quantity',command=self.max_selected_quantity).pack(side='left',padx=3)
        ttk.Button(tools,text='Max All Stacks in Category',command=self.max_all_quantities).pack(side='left',padx=3)
        ttk.Button(tools,text='Restore Selected Durability',command=self.restore_selected_durability).pack(side='left',padx=3)
        ttk.Button(tools,text='Set Infinite Durability',command=self.infinite_selected_durability).pack(side='left',padx=3)
        self.inv_hint=tk.StringVar(value='Select an inventory slot to see item-specific tools.')
        ttk.Label(tools,textvariable=self.inv_hint,style='Muted.TLabel').pack(side='right',padx=8)

        pane=ttk.Panedwindow(self.inv_tab,orient='horizontal'); pane.pack(fill='both',expand=True)
        left=ttk.Frame(pane); right=ttk.Frame(pane,padding=(10,0,0,0)); pane.add(left,weight=3); pane.add(right,weight=2)
        self.inv_tree=ttk.Treeview(left,columns=('slot','name','id'),show='headings',selectmode='browse')
        self.inv_tree.heading('slot',text='Slot'); self.inv_tree.heading('name',text='Item'); self.inv_tree.heading('id',text='Internal ID')
        self.inv_tree.column('slot',width=55,anchor='center'); self.inv_tree.column('name',width=230); self.inv_tree.column('id',width=260)
        ys=ttk.Scrollbar(left,orient='vertical',command=self.inv_tree.yview); self.inv_tree.configure(yscrollcommand=ys.set)
        self.inv_tree.pack(side='left',fill='both',expand=True); ys.pack(side='right',fill='y')
        self.inv_tree.bind('<<TreeviewSelect>>',lambda e:self.load_slot_fields())
        self.field_frame=ttk.LabelFrame(right,text='Selected Item Details',padding=12); self.field_frame.pack(fill='both',expand=True)
        self.inv_field_vars={}
        ttk.Button(right,text='Apply Item Changes',command=self.apply_slot_fields,style='Accent.TButton').pack(anchor='w',pady=8)

    def _build_completion(self):
        header=ttk.Frame(self.comp_tab); header.pack(fill='x',pady=(0,8))
        ttk.Label(header,text='Completion Dashboard',style='SectionTitle.TLabel').pack(side='left')
        ttk.Button(header,text='Refresh Counts',command=self.refresh_completion).pack(side='right')
        ttk.Label(self.comp_tab,text='Only variables that already exist in the loaded KTML are changed. Missing hashes are never invented.',style='Muted.TLabel').pack(anchor='w',pady=(0,8))

        cols=('category','complete','available','total')
        self.comp_tree=ttk.Treeview(self.comp_tab,columns=cols,show='headings',selectmode='browse',height=13)
        for col,text,w in [('category','Category',310),('complete','Complete',90),('available','Keys in this save',120),('total','Reference total',105)]:
            self.comp_tree.heading(col,text=text); self.comp_tree.column(col,width=w,anchor='w' if col=='category' else 'center')
        self.comp_tree.pack(fill='both',expand=True)

        actions=ttk.Frame(self.comp_tab); actions.pack(fill='x',pady=8)
        ttk.Button(actions,text='Mark Existing Keys Complete',command=lambda:self.set_completion_group(True),style='Accent.TButton').pack(side='left')
        ttk.Button(actions,text='Clear Existing Keys',command=lambda:self.set_completion_group(False)).pack(side='left',padx=6)
        self.comp_note=tk.StringVar(value='Select a category. Write actions require confirmation.')
        ttk.Label(actions,textvariable=self.comp_note,style='Muted.TLabel').pack(side='left',padx=12)

    def _on_editor_tab_changed(self, _event=None):
        """Short event-driven gold sweep for inner editor tabs; no idle animation/CPU use."""
        self._animate_editor_glint(0)

    def _animate_editor_glint(self, step=0):
        if not hasattr(self,'editor_glint') or not self.editor_glint.winfo_exists(): return
        c=self.editor_glint; c.delete('all')
        width=max(1,c.winfo_width())
        x=int(width*(step/12.0))
        c.create_line(max(0,x-70),2,x,2,fill=self.accent,width=3)
        c.create_oval(max(0,x-3),0,x+3,4,fill='#FFF0A8',outline='')
        if step<12: c.after(14,lambda:self._animate_editor_glint(step+1))
        else: c.after(30,lambda:c.delete('all'))

    def _build_autobuilder(self):
        header=ttk.Frame(self.auto_tab); header.pack(fill='x',pady=(0,8))
        ttk.Label(header,text='Autobuilder Draft Inspector',style='SectionTitle.TLabel').pack(side='left')
        ttk.Button(header,text='Refresh',command=self.refresh_autobuilder).pack(side='right')
        ttk.Label(
            self.auto_tab,
            text='Reference support from Marc Robledo’s Autobuilder research. Sacred Realms safely inspects draft index/camera arrays when they exist. Binary CombinedActorInfo payloads are intentionally not rewritten by this KTML editor.',
            style='Muted.TLabel',wraplength=980,
        ).pack(anchor='w',pady=(0,8))
        cols=('slot','draft','camera_pos','camera_at','binary')
        self.auto_tree=ttk.Treeview(self.auto_tab,columns=cols,show='headings',selectmode='browse',height=16)
        for col,label,width in [
            ('slot','Slot',60),('draft','Draft Index',90),('camera_pos','Camera Position',260),
            ('camera_at','Camera Target',260),('binary','CombinedActorInfo',170),
        ]:
            self.auto_tree.heading(col,text=label); self.auto_tree.column(col,width=width,anchor='center' if col in ('slot','draft') else 'w')
        self.auto_tree.pack(fill='both',expand=True)
        self.auto_note=tk.StringVar(value='Load a KTML containing Autobuilder.Draft.* arrays to inspect drafts.')
        ttk.Label(self.auto_tab,textvariable=self.auto_note,style='Muted.TLabel').pack(anchor='w',pady=(8,0))

    def refresh_autobuilder(self):
        if not hasattr(self,'auto_tree'): return
        for x in self.auto_tree.get_children(): self.auto_tree.delete(x)
        if not self.doc: return
        indexes=self._find('AutoBuilder.Draft.Content.Index',None)
        cams=self._find('AutoBuilder.Draft.Content.CameraPos',None)
        ats=self._find('AutoBuilder.Draft.Content.CameraAt',None)
        binary=self._find('AutoBuilder.Draft.Content.CombinedActorInfo',None)
        arrays=[x for x in (indexes,cams,ats,binary) if isinstance(x,list)]
        if not arrays:
            self.auto_note.set('This client KTML does not contain Autobuilder draft arrays. Nothing was changed.')
            return
        count=max(len(x) for x in arrays)
        populated=0
        def fmtvec(v):
            if isinstance(v,dict):
                try: return f"{float(v.get('x',0)):.2f}, {float(v.get('y',0)):.2f}, {float(v.get('z',0)):.2f}"
                except Exception: return str(v)
            return '—'
        for i in range(count):
            draft=indexes[i] if isinstance(indexes,list) and i<len(indexes) else '—'
            cp=cams[i] if isinstance(cams,list) and i<len(cams) else None
            ca=ats[i] if isinstance(ats,list) and i<len(ats) else None
            bp=binary[i] if isinstance(binary,list) and i<len(binary) else None
            is_populated=isinstance(draft,int) and draft>=0
            if is_populated: populated+=1
            if bp is None: btxt='not present'
            elif isinstance(bp,(list,dict)) and len(bp)==0: btxt='empty/preserved'
            else:
                try: btxt=f'{len(bp):,} value(s) • preserved'
                except Exception: btxt='present • preserved'
            self.auto_tree.insert('', 'end', iid=str(i), values=(i,draft,fmtvec(cp),fmtvec(ca),btxt))
        self.auto_note.set(f'{count} draft slot(s) detected • {populated} populated index slot(s). Binary payloads are preserved rather than rewritten.')

    def _build_advanced(self):
        top=ttk.Frame(self.adv_tab); top.pack(fill='x',pady=(0,8))
        ttk.Label(top,text='Search:').pack(side='left')
        self.adv_search=tk.StringVar(); e=ttk.Entry(top,textvariable=self.adv_search); e.pack(side='left',fill='x',expand=True,padx=6); e.bind('<KeyRelease>',lambda ev:self.refresh_advanced())
        ttk.Button(top,text='Refresh',command=self.refresh_advanced).pack(side='left')
        self.adv_tree=ttk.Treeview(self.adv_tab,columns=('section','path','value'),show='headings',selectmode='browse')
        for col,text,w in [('section','Type',110),('path','Variable path',470),('value','Value / structure',300)]: self.adv_tree.heading(col,text=text); self.adv_tree.column(col,width=w)
        self.adv_tree.pack(fill='both',expand=True)
        self.adv_tree.bind('<<TreeviewSelect>>',lambda e:self._advanced_selected())

        bottom=ttk.LabelFrame(self.adv_tab,text='Selected Value',padding=8); bottom.pack(fill='x',pady=8)
        self.adv_index=tk.StringVar(value='0'); self.adv_value=tk.StringVar()
        ttk.Label(bottom,text='Array index:').pack(side='left'); ttk.Entry(bottom,textvariable=self.adv_index,width=8).pack(side='left',padx=6)
        ttk.Button(bottom,text='Load Index',command=self.load_advanced_index).pack(side='left')
        ttk.Label(bottom,text='Value:').pack(side='left',padx=(12,0)); ttk.Entry(bottom,textvariable=self.adv_value,width=44).pack(side='left',padx=6,fill='x',expand=True)
        ttk.Button(bottom,text='Apply Variable / Array Element',command=self.apply_advanced,style='Accent.TButton').pack(side='left')

    # ---------- document helpers ----------
    def _find(self,path,default=None):
        if not self.doc: return default
        for section in self.doc.values():
            if isinstance(section,dict) and path in section: return section[path]
        return default

    def _locate(self,path):
        if not self.doc: return None
        for sec,section in self.doc.items():
            if isinstance(section,dict) and path in section: return sec,section
        return None

    def _set(self,path,value):
        loc=self._locate(path)
        if loc:
            loc[1][path]=value; self._dirty(); return True
        return False

    def _dirty(self):
        self.dirty=True; self._hash_cache=None
        if self.path: self.file_label.config(text=f'{self.path.name} • unsaved changes')

    def _build_hash_index(self):
        if self._hash_cache is not None: return self._hash_cache
        idx={}
        if self.doc:
            for sec,section in self.doc.items():
                if not isinstance(section,dict): continue
                for path in section:
                    try: idx[key_hash(path)] = (sec,path)
                    except Exception: pass
        self._hash_cache=idx
        return idx

    # ---------- open/save ----------
    def load_path(self,path):
        try:
            text=Path(path).read_text(encoding='utf-8',errors='strict')
            self.doc=parse_ktml(text); self.path=Path(path); self.dirty=False; self._hash_cache=None
            self.repair_arrow_equip_index()
            self.refresh_all(); self.file_label.config(text=self.path.name)
            if hasattr(self,'world_file_var'): self.world_file_var.set(self.path.name)
        except Exception as exc:
            messagebox.showerror('Cannot open KTML',str(exc),parent=self)

    def open_file(self):
        p=filedialog.askopenfilename(parent=self,title='Open client KTML',filetypes=[('KTML / text','*.ktml *.txt'),('All files','*.*')])
        if p: self.load_path(p)

    def load_default(self):
        candidates=[_resource_path('defaultClientSave.ktml'),_resource_path('server_bundle/User/Resources/SaveServer/defaultClientSave.ktml')]
        for p in candidates:
            if p.exists(): self.load_path(p); return
        messagebox.showerror('Default not found','The bundled Kirbymimi defaultClientSave.ktml was not found.',parent=self)

    def backup_current(self):
        if not self.path or not self.path.exists(): messagebox.showwarning('No file','Load a KTML file first.',parent=self); return
        p=filedialog.asksaveasfilename(parent=self,title='Backup client save',defaultextension='.ktml',initialfile=self.path.stem+'-BACKUP.ktml',filetypes=[('KTML','*.ktml')])
        if p: shutil.copy2(self.path,p); messagebox.showinfo('Backup created',f'Backup saved to:\n{p}',parent=self)

    def save_as(self):
        if not self.doc: messagebox.showwarning('No file','Load a KTML file first.',parent=self); return
        self.repair_arrow_equip_index()
        probs=self.validate_document()
        if probs:
            messagebox.showerror('Save blocked','The editor found problems that could create a broken client save:\n\n'+'\n'.join(probs[:10]),parent=self); return
        try: validate_ktml_types(self.doc)
        except Exception as exc: messagebox.showerror('Save blocked',f'KTML type validation failed:\n{exc}',parent=self); return
        p=filedialog.asksaveasfilename(parent=self,title='Save edited client KTML',defaultextension='.ktml',initialfile=(self.path.stem if self.path else 'defaultClientSave')+'-edited.ktml',filetypes=[('KTML','*.ktml')])
        if not p: return
        with Path(p).open('w',encoding='utf-8',newline='') as f:
            f.write(serialize_ktml(self.doc))
        self.path=Path(p); self.dirty=False; self.file_label.config(text=self.path.name)
        if hasattr(self,'world_file_var'): self.world_file_var.set(self.path.name)
        messagebox.showinfo('Saved','Edited KTML saved. Keep your original backup until the edited save is tested successfully.',parent=self)

    # ---------- status ----------
    def refresh_all(self):
        if not self.doc: return
        for path,(var,typ) in self.status_vars.items(): var.set(str(self._find(path,'')))
        for path,var in self.sage_vars.items(): var.set(bool(self._find(path,False)))
        pos=self._find('World_PlayerPos',None) or self._find('PlayerStatus.SavePos',{})
        if isinstance(pos,dict):
            for axis in ('x','y','z'): self.pos_vars[axis].set(str(pos.get(axis,'')))
            self.map_layer_choice.set(self._layer_from_height(pos.get('y',0)))
        self._refresh_map_layer(); self._draw_world_map()
        self.refresh_inventory(); self.refresh_completion(); self.refresh_autobuilder(); self.refresh_advanced()

    def apply_status(self):
        if not self.doc: return
        try:
            for path,(var,typ) in self.status_vars.items():
                if self._locate(path): self._set(path,float(var.get()) if typ=='Float' else int(var.get()))
            messagebox.showinfo('Applied','Player status values updated in memory. Use Save Edited Copy to write the KTML.',parent=self)
        except ValueError: messagebox.showerror('Invalid number','One of the player values is not a valid number.',parent=self)

    def _apply_bool(self,path):
        if self.doc and self._locate(path): self._set(path,bool(self.sage_vars[path].get()))

    def _refresh_map_layer(self):
        try: y=float(self.pos_vars['y'].get())
        except Exception: self.map_layer_var.set('Map Layer: —'); return
        layer=self._layer_from_height(y)
        self.map_layer_var.set(f"Map Layer: {layer if layer != 'Surface' else 'Surface / Hyrule'}")

    def apply_position(self):
        if not self.doc: return
        try: vec={a:float(self.pos_vars[a].get()) for a in ('x','y','z')}
        except ValueError: messagebox.showerror('Invalid position','X, Y and Z must all be numbers.',parent=self); return
        layer=self._layer_from_height(vec['y'])
        if not messagebox.askyesno('Apply world position?',f"Write Link's position to the loaded KTML?\n\nX: {vec['x']:.3f}\nY: {vec['y']:.3f}\nZ: {vec['z']:.3f}\nLayer: {layer}\n\nThis updates World_PlayerPos and PlayerStatus.SavePos when those fields exist. Keep your original backup until the rescue position is tested.",parent=self): return
        changed=[]
        for path in ('World_PlayerPos','PlayerStatus.SavePos'):
            if self._locate(path): self._set(path,dict(vec)); changed.append(path)
        self._refresh_map_layer(); self._draw_world_map()
        if changed:
            self.map_status_var.set('Position written in memory to: '+', '.join(changed)+'. Use Save Edited Copy before testing.')
            messagebox.showinfo('Position updated','Link’s world position was updated in memory. Now use Save Edited Copy to create the rescued KTML.',parent=self)
        else: messagebox.showwarning('Position fields missing','This KTML does not contain World_PlayerPos or PlayerStatus.SavePos.',parent=self)

    def _set_or_add_typed(self, section_name, path, value):
        """Set a KTML value in a known type section, adding only that exact verified key if absent."""
        if not self.doc:
            return False
        section=self.doc.setdefault(section_name,{})
        if not isinstance(section,dict):
            return False
        section[path]=value
        self._dirty()
        return True

    def apply_multiplayer_spawn_recovery(self):
        """Conservative recovery for the observed multiplayer fall/respawn loop.

        Evidence from the supplied logs shows the affected save reaches MainField, then repeatedly
        writes HavePlayedEvent.DmF_SY_FallDownReturn while a clean save works.  This recovery does
        not touch inventory, quests, completion, or equipment.  It resets only the two observed
        fall-event flags, the current field, the two position vectors, rotation, and the known
        respawn-location string array used in the same log sequence.
        """
        if not self.doc:
            messagebox.showwarning('No client save','Open the affected client KTML first.',parent=self)
            return
        target={'x':-254.12,'y':126.45,'z':-101.60}
        msg=(
            'This is a targeted recovery for the multiplayer fall loop seen after Actor Sync reaches MainField.\n\n'
            'It will NOT erase inventory, equipment, quests, completion, or normal player stats.\n\n'
            'It will set both player position fields to Lookout Landing, reset the two observed '
            'FallDown event flags, force MainField, clear saved facing rotation, and reset the '
            'observed respawn-location string array to City_BaseCamp / BaseCamp_Shelter.\n\n'
            'You already made a backup, but keep it until this recovery is tested. Continue?'
        )
        if not messagebox.askyesno('Build multiplayer spawn recovery?',msg,parent=self):
            return

        # Position and field state.
        for path in ('World_PlayerPos','PlayerStatus.SavePos'):
            loc=self._locate(path)
            if loc:
                loc[1][path]=dict(target)
            else:
                self._set_or_add_typed('Vector3',path,dict(target))
        self._set_or_add_typed('Float','PlayerStatus.SavePosRadY',0.0)
        self._set_or_add_typed('String64','Sequence_CurrentBanc','MainField')

        # These exact flags are the ones repeatedly written immediately before the observed fall loop.
        self._set_or_add_typed('Bool','HavePlayedEvent.DmF_SY_FallDown',False)
        self._set_or_add_typed('Bool','HavePlayedEvent.DmF_SY_FallDownReturn',False)

        # The server log showed this array carrying the current/return location names.  Preserve
        # the original array length and reset only the front entries needed for Lookout Landing.
        arr=self._find('unk3294163435',None)
        if not isinstance(arr,list):
            arr=['']*20
        else:
            arr=list(arr)
            if len(arr)<20: arr += ['']*(20-len(arr))
        arr[0]='City_BaseCamp'; arr[1]='BaseCamp_Shelter'
        for i in range(2,min(len(arr),20)): arr[i]=''
        self._set_or_add_typed('String64Array','unk3294163435',arr)

        # Location flags seen in the successful Lookout Landing load sequence.
        self._set_or_add_typed('Bool','IsVisitLocation.City_BaseCamp',True)
        self._set_or_add_typed('Bool','IsVisitLocation.BaseCamp_Shelter',True)

        self._dirty()
        for axis,val in target.items(): self.pos_vars[axis].set(str(val))
        self.map_layer_choice.set('Surface')
        self._refresh_map_layer(); self._refresh_layer_cards(); self._draw_world_map()
        self.map_status_var.set('Multiplayer spawn recovery prepared in memory: Lookout Landing + fall/return context reset. Save Edited Copy, replace the player KTML while the realm is STOPPED, then start/reconnect.')
        messagebox.showinfo('Recovery prepared','Recovery changes are in memory.\n\nNext: Save Edited Copy → stop the realm → replace the affected player KTML → start the realm → reconnect.\n\nIf the fall loop still starts only after MainField, the remaining fault is in the multiplayer runtime rather than the saved spawn context.',parent=self)

    # ---------- inventory ----------
    def _cat(self): return CATEGORY_INFO[self.category_var.get()]
    def _names_path(self,info): return info['section']+'.Content.Name'

    def refresh_inventory(self):
        for x in self.inv_tree.get_children(): self.inv_tree.delete(x)
        cat=self.category_var.get(); info=CATEGORY_INFO[cat]
        names=self._find(self._names_path(info),[]) if self.doc else []
        cmap=self.catalog_names.get(cat,{})
        if isinstance(names,list):
            for i,item_id in enumerate(names): self.inv_tree.insert('', 'end', iid=str(i), values=(i,cmap.get(item_id,item_id or '<empty>'),item_id))
        values=[]
        for item in self.catalog.get('categories',{}).get(cat,[]): values.append(f"{item.get('name',item.get('id',''))}  |  {item.get('id','')}")
        if cat=='abilities' and not values:
            values=[f'{x}  |  {x}' for x in self.reference.get('abilities',[])]
        self.item_combo['values']=values
        self._rebuild_slot_fields(); self.inv_hint.set(f"{info['label']}: {sum(bool(x) for x in names) if isinstance(names,list) else 0} populated slot(s)")

    def _selected_index(self):
        sel=self.inv_tree.selection(); return int(sel[0]) if sel else None

    def _rebuild_slot_fields(self):
        for w in self.field_frame.winfo_children(): w.destroy()
        self.inv_field_vars={}; info=self._cat()
        for i,(prop,path,typ,label) in enumerate(info.get('fields',[])):
            ttk.Label(self.field_frame,text=label).grid(row=i,column=0,sticky='w',pady=3)
            v=tk.StringVar(); self.inv_field_vars[path]=(v,typ)
            if typ=='enum':
                widget=ttk.Combobox(self.field_frame,textvariable=v,values=[f'{n} | {self.catalog.get("enumHashes",{}).get(n,key_hash(n))}' for n in MODIFIER_NAMES],width=34)
            else:
                widget=ttk.Entry(self.field_frame,textvariable=v)
            widget.grid(row=i,column=1,sticky='ew',padx=6,pady=3)
        self.field_frame.columnconfigure(1,weight=1)

    def load_slot_fields(self):
        idx=self._selected_index()
        if idx is None: return
        info=self._cat(); names=self._find(self._names_path(info),[])
        item_id=names[idx] if isinstance(names,list) and idx<len(names) else ''
        for path,(var,typ) in self.inv_field_vars.items():
            arr=self._find(path,[]); value=arr[idx] if isinstance(arr,list) and idx<len(arr) else ''
            if typ=='enum' and str(value) in self.reverse_enums: var.set(f'{self.reverse_enums[str(value)]} | {value}')
            else: var.set(str(value))
        self.inv_hint.set(f"Selected: {self.catalog_names.get(self.category_var.get(),{}).get(item_id,item_id or '<empty>')}")

    def _item_id_from_entry(self,text):
        text=text.strip()
        if '|' in text: return text.rsplit('|',1)[1].strip()
        cat=self.category_var.get(); return self.catalog_ids_by_name.get(cat,{}).get(text.lower(),text)

    def put_item(self):
        if not self.doc: return
        idx=self._selected_index()
        if idx is None: messagebox.showwarning('Select a slot','Choose an inventory slot first.',parent=self); return
        item_id=self._item_id_from_entry(self.item_var.get())
        if not item_id: messagebox.showwarning('Choose an item','Choose or enter an item ID first.',parent=self); return
        info=self._cat(); names=self._find(self._names_path(info),[])
        if not isinstance(names,list) or idx>=len(names): return
        names[idx]=item_id
        for prop,path,typ,label in info.get('fields',[]):
            arr=self._find(path,[])
            if isinstance(arr,list) and idx<len(arr):
                if prop=='quantity': arr[idx]=self._max_quantity_for(item_id, self.category_var.get())
                elif prop=='durability': arr[idx]=self._default_durability(item_id)
                elif typ=='number': arr[idx]=(-1 if prop not in ('getOrder','useOrder') else idx)
                elif typ=='item': arr[idx]=''
                elif typ=='enum': arr[idx]=self.catalog.get('enumHashes',{}).get('None',key_hash('None'))
        self.repair_arrow_equip_index(); self._dirty(); self.refresh_inventory(); self.inv_tree.selection_set(str(idx)); self.inv_tree.see(str(idx)); self.load_slot_fields()

    def clear_slot(self):
        if not self.doc: return
        idx=self._selected_index(); info=self._cat()
        if idx is None: return
        names=self._find(self._names_path(info),[])
        if isinstance(names,list) and idx<len(names): names[idx]=''
        for prop,path,typ,label in info.get('fields',[]):
            arr=self._find(path,[])
            if isinstance(arr,list) and idx<len(arr): arr[idx]=(-1 if typ in ('number','enum') else '')
        self.repair_arrow_equip_index(); self._dirty(); self.refresh_inventory()

    def _parse_field(self,raw,typ):
        raw=raw.strip()
        if typ=='enum' and '|' in raw: raw=raw.rsplit('|',1)[1].strip()
        if typ in ('number','enum'):
            if re.fullmatch(r'-?\d+',raw or ''): return int(raw)
            return float(raw)
        return raw

    def apply_slot_fields(self):
        if not self.doc: return
        idx=self._selected_index()
        if idx is None: return
        try:
            for path,(var,typ) in self.inv_field_vars.items():
                arr=self._find(path,[])
                if not isinstance(arr,list) or idx>=len(arr): continue
                arr[idx]=self._parse_field(var.get(),typ)
            self.repair_arrow_equip_index(); self._dirty(); self.refresh_inventory(); self.inv_tree.selection_set(str(idx)); self.load_slot_fields()
        except ValueError as exc: messagebox.showerror('Invalid field',str(exc),parent=self)

    def _max_quantity_for(self,item_id,category=None):
        if category == 'key' and item_id not in set(self.reference.get('keyCountable',[])):
            return -1
        specific=self.reference.get('itemMaximumQuantity',{}).get(item_id)
        if specific is not None: return int(specific)
        return 999

    def _default_durability(self,item_id):
        return int(self.catalog.get('durability',{}).get(item_id, self.reference.get('defaultDurability',{}).get(item_id,70)))

    def max_selected_quantity(self):
        idx=self._selected_index(); info=self._cat()
        if idx is None: return
        qfield=next((x for x in info.get('fields',[]) if x[0]=='quantity'),None)
        if not qfield: messagebox.showinfo('Not stackable','This category does not have a quantity field.',parent=self); return
        names=self._find(self._names_path(info),[]); arr=self._find(qfield[1],[])
        if not isinstance(names,list) or not isinstance(arr,list) or idx>=len(arr): return
        if not names[idx]: return
        arr[idx]=self._max_quantity_for(names[idx], self.category_var.get()); self._dirty(); self.refresh_inventory(); self.inv_tree.selection_set(str(idx)); self.load_slot_fields()

    def max_all_quantities(self):
        if not self.doc: return
        info=self._cat(); qfield=next((x for x in info.get('fields',[]) if x[0]=='quantity'),None)
        if not qfield: messagebox.showinfo('Not stackable','This category does not have a quantity field.',parent=self); return
        names=self._find(self._names_path(info),[]); arr=self._find(qfield[1],[])
        if not isinstance(names,list) or not isinstance(arr,list): return
        changed=0
        for i,item_id in enumerate(names):
            if item_id and i<len(arr): arr[i]=self._max_quantity_for(item_id, self.category_var.get()); changed+=1
        if changed: self._dirty(); self.refresh_inventory(); messagebox.showinfo('Quantities updated',f'Updated {changed} populated stack(s) using the known per-item maximum where available.',parent=self)

    def restore_selected_durability(self):
        cat=self.category_var.get(); idx=self._selected_index()
        if cat not in ('weapons','bows','shields') or idx is None:
            messagebox.showinfo('Equipment only','Select a weapon, bow, or shield first.',parent=self); return
        info=self._cat(); names=self._find(self._names_path(info),[])
        if not isinstance(names,list) or idx>=len(names) or not names[idx]: return
        life=self._find(info['section']+'.Content.Life',[])
        if not isinstance(life,list) or idx>=len(life): return
        base=self._default_durability(names[idx])
        mod=self._find(info['section']+'.Content.Effect.Type',[]); modv=self._find(info['section']+'.Content.Effect.Value',[])
        durup={self.catalog.get('enumHashes',{}).get('DurabilityUp',key_hash('DurabilityUp')),self.catalog.get('enumHashes',{}).get('DurabilityUpPlus',key_hash('DurabilityUpPlus'))}
        if isinstance(mod,list) and idx<len(mod) and mod[idx] in durup and isinstance(modv,list) and idx<len(modv):
            try: base += max(0,int(modv[idx]))
            except Exception: pass
        life[idx]=base
        if cat in ('weapons','shields'):
            fuse_names=self._find(info['section']+'.Content.Combined.Name',[])
            fuse_life=self._find(info['section']+'.Content.Combined.Life',[])
            if isinstance(fuse_names,list) and idx<len(fuse_names) and fuse_names[idx] and isinstance(fuse_life,list) and idx<len(fuse_life):
                known_durability = self.catalog.get('durability',{})
                reference_durability = self.reference.get('defaultDurability',{})
                if fuse_names[idx] in known_durability or fuse_names[idx] in reference_durability:
                    fuse_life[idx]=self._default_durability(fuse_names[idx])
            if cat=='weapons':
                fused = bool(isinstance(fuse_names,list) and idx<len(fuse_names) and fuse_names[idx])
                bonus=int(self.catalog.get('fuseDurability',{}).get(names[idx],self.reference.get('fuseDurability',{}).get(names[idx],25)))
                extra=self._find('Pouch.Weapon.Content.ExtraLife',[])
                record=self._find('Pouch.Weapon.Content.RecordExtraLife',[])
                if isinstance(extra,list) and idx<len(extra): extra[idx]=bonus if fused else 0
                if isinstance(record,list) and idx<len(record): record[idx]=bonus if fused else -1
        self._dirty(); self.refresh_inventory(); self.inv_tree.selection_set(str(idx)); self.load_slot_fields()

    def infinite_selected_durability(self):
        cat=self.category_var.get(); idx=self._selected_index()
        if cat not in ('weapons','bows','shields') or idx is None:
            messagebox.showinfo('Equipment only','Select a weapon, bow, or shield first.',parent=self); return
        if not messagebox.askyesno('Set infinite durability?','This uses the referenced editor behavior: DurabilityUpPlus with modifier value 2,100,000,000.\n\nBack up your save before testing. Continue?',parent=self): return
        info=self._cat(); mod=self._find(info['section']+'.Content.Effect.Type',[]); modv=self._find(info['section']+'.Content.Effect.Value',[])
        if not (isinstance(mod,list) and isinstance(modv,list) and idx<len(mod) and idx<len(modv)): return
        mod[idx]=self.catalog.get('enumHashes',{}).get('DurabilityUpPlus',key_hash('DurabilityUpPlus')); modv[idx]=2100000000
        self.restore_selected_durability()

    def repair_arrow_equip_index(self):
        names=self._find('Pouch.Arrow.Content.Name',[]); equip=self._find('Pouch.Arrow.EquipIndex',[])
        if not isinstance(names,list) or not isinstance(equip,list) or not equip: return False
        first=next((i for i,x in enumerate(names) if x),-1); changed=False
        if first<0:
            if equip[0]!=-1: equip[0]=-1; changed=True
        else:
            cur=equip[0]
            if not isinstance(cur,int) or cur<0 or cur>=len(names) or not names[cur]: equip[0]=first; changed=True
        if changed: self._dirty()
        return changed

    def validate_document(self):
        probs=[]
        if not self.doc: return ['No document loaded.']
        for cat,info in CATEGORY_INFO.items():
            names=self._find(self._names_path(info),None)
            # Some client-save layouts intentionally omit optional categories.
            if names is None: continue
            if not isinstance(names,list): probs.append(f"{info['label']}: malformed {self._names_path(info)}"); continue
            for _,path,_,_ in info.get('fields',[]):
                arr=self._find(path,None)
                if not isinstance(arr,list): probs.append(f"{info['label']}: missing {path}")
                elif len(arr)!=len(names): probs.append(f'{path}: {len(arr)} values, expected {len(names)}')
            if info.get('recipe'):
                arr=self._find(info['recipe'],None)
                if not isinstance(arr,list) or len(arr)!=len(names)*5: probs.append(f"{info['recipe']}: expected {len(names)*5} values")
            try: first=names.index('')
            except ValueError: first=-1
            if first!=-1 and any(names[first+1:]): probs.append(f"{info['label']}: empty slot exists before a populated slot")
        return probs

    # ---------- completion ----------
    def refresh_completion(self):
        if not hasattr(self,'comp_tree'): return
        for x in self.comp_tree.get_children(): self.comp_tree.delete(x)
        if not self.doc: return
        idx=self._build_hash_index()
        for key,info in self.reference.get('completionGroups',{}).items():
            hashes=info.get('hashes',[]); available=0; complete=0
            for h in hashes:
                loc=idx.get(int(h))
                if not loc: continue
                available+=1
                val=self.doc.get(loc[0],{}).get(loc[1])
                if bool(val): complete+=1
            self.comp_tree.insert('', 'end', iid=key, values=(info.get('label',key),complete,available,len(hashes)))

    def set_completion_group(self,value):
        if not self.doc: return
        sel=self.comp_tree.selection()
        if not sel: messagebox.showinfo('Select a category','Select one completion category first.',parent=self); return
        key=sel[0]; info=self.reference.get('completionGroups',{}).get(key,{})
        action='mark complete' if value else 'clear'
        if not messagebox.askyesno('Confirm completion edit',f"{action.title()} all existing keys for:\n\n{info.get('label',key)}\n\nOnly keys already present in this KTML will be changed. Continue?",parent=self): return
        idx=self._build_hash_index(); changed=0; skipped=0
        for h in info.get('hashes',[]):
            loc=idx.get(int(h))
            if not loc: skipped+=1; continue
            section=self.doc.get(loc[0],{})
            old=section.get(loc[1])
            if isinstance(old,bool): section[loc[1]]=bool(value); changed+=1
            elif isinstance(old,int) and old in (0,1): section[loc[1]]=1 if value else 0; changed+=1
            else: skipped+=1
        if changed: self._dirty()
        self.refresh_completion(); self.comp_note.set(f'{changed} existing boolean key(s) changed; {skipped} unavailable/non-boolean key(s) skipped.')

    # ---------- advanced ----------
    def refresh_advanced(self):
        for x in self.adv_tree.get_children(): self.adv_tree.delete(x)
        if not self.doc: return
        q=self.adv_search.get().lower().strip(); count=0
        for sec,values in self.doc.items():
            if not isinstance(values,dict): continue
            for path,val in values.items():
                if q and q not in f'{sec} {path}'.lower(): continue
                if isinstance(val,dict): shown=f'<object: {len(val)} values>'
                elif isinstance(val,list): shown=f'<array: {len(val)} values>'
                else: shown=str(val)
                iid=f'{sec}\x1f{path}'; self.adv_tree.insert('', 'end', iid=iid, values=(sec,path,shown)); count+=1
                if count>=5000: return

    def _advanced_selected(self):
        sel=self.adv_tree.selection()
        if not sel: return
        sec,path=sel[0].split('\x1f',1); val=self.doc[sec][path]
        self.adv_index.set('0')
        if isinstance(val,list): self.load_advanced_index()
        elif isinstance(val,dict): self.adv_value.set(json.dumps(val,ensure_ascii=False))
        else: self.adv_value.set(str(val).lower() if isinstance(val,bool) else str(val))

    def load_advanced_index(self):
        sel=self.adv_tree.selection()
        if not sel or not self.doc: return
        sec,path=sel[0].split('\x1f',1); val=self.doc[sec][path]
        if not isinstance(val,list): return
        try: i=int(self.adv_index.get())
        except ValueError: return
        if 0<=i<len(val):
            cell=val[i]; self.adv_value.set(json.dumps(cell,ensure_ascii=False) if isinstance(cell,(dict,list)) else str(cell).lower() if isinstance(cell,bool) else str(cell))

    @staticmethod
    def _convert_like(raw,old):
        raw=raw.strip()
        if isinstance(old,bool): return raw.lower() in ('true','1','yes','on')
        if isinstance(old,int) and not isinstance(old,bool): return int(raw)
        if isinstance(old,float): return float(raw)
        if isinstance(old,dict):
            value=json.loads(raw)
            if not isinstance(value,dict): raise ValueError('Expected a JSON object.')
            return value
        if isinstance(old,list):
            value=json.loads(raw)
            if not isinstance(value,list): raise ValueError('Expected a JSON array.')
            return value
        return raw

    def apply_advanced(self):
        sel=self.adv_tree.selection()
        if not sel or not self.doc: return
        sec,path=sel[0].split('\x1f',1); old=self.doc[sec][path]; raw=self.adv_value.get()
        try:
            if isinstance(old,list):
                i=int(self.adv_index.get())
                if not (0<=i<len(old)): raise ValueError(f'Array index must be between 0 and {max(0,len(old)-1)}.')
                old[i]=self._convert_like(raw,old[i])
            else:
                self.doc[sec][path]=self._convert_like(raw,old)
            self._dirty(); self.refresh_advanced()
        except (ValueError,TypeError,json.JSONDecodeError) as exc:
            messagebox.showerror('Invalid value',str(exc),parent=self)
