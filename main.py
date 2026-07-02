# RT-BDS
# Room Temperature Broadband Dielectric Spectroscopy.
# Room-level LCR frequency sweeps across up to 16 probe relays.

# imports
from dataclasses import dataclass
import colorsys
from datetime import datetime
from enum import IntFlag, auto
import os
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
import numpy as np
import pandas as pd

try:
    import devices as devices  # type: ignore
except Exception as e:
    raise ImportError(f"Could not import devices_rtbds.py: {e}")

# output path
RUNNING_PATH = os.path.abspath(os.getcwd())
OUTPUT_FOLDER = "RT-BDS Data"
OUTPUT_FILEPATH = os.path.join(RUNNING_PATH, OUTPUT_FOLDER)
os.makedirs(OUTPUT_FILEPATH, exist_ok=True)
DEVICE_NAMES = [dev.name for dev in devices.DEVICE_TYPE_LIST]

# units & constants
CHAR_OHM = "\u03a9"
EPSILON_0_F_PER_M = 8.8541878128e-12  # perm of free space

FREQ_STEP_COLUMNS = ("Step #", "Frequency [Hz]", "*")
DIELECTRIC_COLUMN = "Dielectric Constant [1]"

TEST_DATA_COLUMNS = (
    "Timestamp",
    "Probe Index",
    "Probe Label",
    "Relay Index",
    "Freq. [Hz]",
    "Cp [F]",
    DIELECTRIC_COLUMN,
    "Df [1]",
    f"ESR [{CHAR_OHM}]",
    "Status",
)

PROBE_TABLE_COLUMNS = (
    "Enabled",
    "Probe Index",
    "Probe Label",
    "Relay Index",
)

# hardware limits and preferences
DEFAULT_FIRST_FREQ_HZ = "100"
DEFAULT_LAST_FREQ_HZ = "100000"
DEFAULT_POINTS_PER_DECADE = "8"
DEFAULT_FOCUS_FREQ_HZ = 1000.0
MEASUREMENT_DISPLAY_ROWS = 50
MAX_PROBES = 16
DEFAULT_PROBE_SETTLING_DELAY = 0.2
DEFAULT_FILM_AREA_MM2 = "78.5"  # based on 1cm diameter
DEFAULT_FILM_THICKNESS_UM = "8"
DEFAULT_ROLL_ID = os.environ.get("RTBDS_ROLL_ID", "")
DEFAULT_OPERATOR = os.environ.get("RTBDS_OPERATOR") or os.environ.get(
    "USERNAME", os.environ.get("USER", "")
)
KEYSIGHT_OVERRANGE_THRESHOLD = 9.8e37

PROBE_COLOR_PALETTE = (
    "#1f77b4",
    "#ff7f0e",
    "#2ca02c",
    "#d62728",
    "#9467bd",
    "#8c564b",
    "#e377c2",
    "#7f7f7f",
    "#bcbd22",
    "#17becf",
)

# explicit state control
class RUN_STATE(IntFlag):
    IDLE = auto()
    PROBE_SWITCHING = auto()
    LCR_MEASURING = auto()
    DONE = auto()
    RUNNING = PROBE_SWITCHING | LCR_MEASURING

# user configs
@dataclass(frozen=True)
class RunConfig:
    probe_configs: tuple[tuple[int, str, int], ...]
    probe_settling_delay: float
    film_area_mm2: float
    film_thickness_um: float

# color getter based on probe index
def get_default_probe_color(probe_index: int) -> str:
    if probe_index <= len(PROBE_COLOR_PALETTE):
        return PROBE_COLOR_PALETTE[probe_index - 1]

    hue = ((probe_index - 1) * 0.618033988749895) % 1.0
    red, green, blue = colorsys.hsv_to_rgb(hue, 0.65, 0.78)
    return f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"

# general UI entries
class Entry(tk.Entry):
    textvariable: tk.Variable

    # selects all when selected and formats when not
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "textvariable" in kwargs:
            self.textvariable = kwargs["textvariable"]
        self.bind("<FocusIn>", self.focus_highlight)
        self.bind("<FocusOut>", self.format_input)

    # select all
    def focus_highlight(self, *args):
        self.selection_range(0, "end")

    # tie to var
    def format_input(self, *args):
        if hasattr(self, "textvariable"):
            self.textvariable.set(self.textvariable.get())

# general table helper
class Table(ttk.Treeview):

    # alternating colors w headers
    def __init__(self, header_widths, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tag_configure("evenrow", background="#E8E8E8")
        self.tag_configure("oddrow", background="#FFFFFF")
        self.set_headings(self.cget("columns"), header_widths)

    # dimensions
    def set_headings(self, column_list, width_list):
        self.column("#0", width=0, stretch=False)
        self.heading("#0", text="", anchor="w")
        for i, col in enumerate(column_list):
            width = width_list[i] if i < len(width_list) else 90
            self.column(col, minwidth=45, width=width, stretch=True, anchor="w")
            self.heading(col, text=col, anchor="w")

    # clear past vals and replace w updated dataframe
    def update_table(self, dataframe: pd.DataFrame):
        """Replace all table rows with rows from a dataframe."""
        self.delete(*self.get_children())
        for i, data in enumerate(dataframe.itertuples(index=False, name=None)):
            tag = "evenrow" if i % 2 == 0 else "oddrow"
            self.insert(parent="", index="end", values=data, tags=tag)

# application
class RTBDSApp:

    # initial state and var values
    def __init__(self, root: tk.Tk):
        self.app_root = root
        self.app_root.title("RT-BDS Testing")
        self.app_root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.closing = False
        self.configure_fonts()
        plt.ioff()
        try:
            self.app_root.state("zoomed")
        except tk.TclError:
            pass

        self.state = RUN_STATE.IDLE
        self.stop_requested = False
        self.stop_event = threading.Event()
        self.run_thread: threading.Thread | None = None
        self.data_lock = threading.Lock()
        self.relay_lock = threading.Lock()
        self.hardware_lock = threading.Lock()
        self.run_progress_completed = 0
        self.run_progress_total = 0

        self.device_list: list[devices.Device] = []

        self.freq_step_data = pd.DataFrame(columns=FREQ_STEP_COLUMNS)
        self.custom_freq_data = pd.DataFrame(columns=FREQ_STEP_COLUMNS)
        self.test_data = pd.DataFrame(columns=TEST_DATA_COLUMNS)

        self.setup_controls: list[tk.Widget] = []
        self.manual_device_controls: list[tk.Widget] = []
        self.manual_probe_controls: list[tk.Widget] = []
        self.idle_controls: list[tk.Widget] = []

        self.first_freq_strvar = tk.StringVar(value=DEFAULT_FIRST_FREQ_HZ)
        self.last_freq_strvar = tk.StringVar(value=DEFAULT_LAST_FREQ_HZ)
        self.points_per_decade_strvar = tk.StringVar(
            value=DEFAULT_POINTS_PER_DECADE
        )
        self.manual_freq_strvar = tk.StringVar(value="")
        self.probe_settling_strvar = tk.StringVar(
            value=str(DEFAULT_PROBE_SETTLING_DELAY)
        )
        self.film_area_mm2_strvar = tk.StringVar(value=DEFAULT_FILM_AREA_MM2)
        self.film_thickness_um_strvar = tk.StringVar(
            value=DEFAULT_FILM_THICKNESS_UM
        )
        self.selected_plot_freq_strvar = tk.StringVar(value="")
        self.roll_id_strvar = tk.StringVar(value=DEFAULT_ROLL_ID)
        self.operator_strvar = tk.StringVar(value=DEFAULT_OPERATOR)
        self.traceability_confirmed = bool(DEFAULT_ROLL_ID and DEFAULT_OPERATOR)

        self.device_strvar = tk.StringVar(value=DEVICE_NAMES[0] if DEVICE_NAMES else "")
        self.message_strvar = tk.StringVar()
        self.response_strvar = tk.StringVar(value="No command sent.")
        self.status_strvar = tk.StringVar(value="Idle")
        self.progress_strvar = tk.StringVar(value="Progress: idle")
        self.progress_value = tk.DoubleVar(value=0.0)
        self.relay_status_strvar = tk.StringVar(value="Relay status not read.")

        self.probe_vars: list[dict[str, tk.Variable]] = []
        for probe_index in range(1, MAX_PROBES + 1):
            probe_vars = {
                "enabled": tk.BooleanVar(value=probe_index == 1),
                "label": tk.StringVar(value=f"Probe {probe_index:02d}"),
                "relay": tk.StringVar(value=str(probe_index)),
            }
            for var in probe_vars.values():
                var.trace_add("write", lambda *_: self.on_probe_setup_changed())
            self.probe_vars.append(probe_vars)

        self.probe_plot_vars = [
            tk.BooleanVar(value=probe_index == 1)
            for probe_index in range(1, MAX_PROBES + 1)
        ]

        self.build_ui()
        self.refresh_probe_table()
        self.update_plot_filter_options()
        self.update_plots()
        self.update_control_states()
        self.debug("App started.")

    # helper for font size and UI
    def configure_fonts(self):
        for font_name, size in (
            ("TkDefaultFont", 16),
            ("TkTextFont", 16),
            ("TkMenuFont", 16),
            ("TkHeadingFont", 20),
            ("TkCaptionFont", 14),
            ("TkSmallCaptionFont", 10),
        ):
            try:
                tkfont.nametofont(font_name).configure(size=size)
            except tk.TclError:
                pass
        self.section_font = ("default", 14, "bold")
        try:
            style = ttk.Style(self.app_root)
            style.configure("TNotebook.Tab", font=("default", 12))
            style.configure("Treeview", font=("default", 14), rowheight=24)
            style.configure("Treeview.Heading", font=("default", 10, "bold"))
            style.configure("TLabel", font=("default", 14))
            style.configure("TButton", font=("default", 14))
            style.configure("TCheckbutton", font=("default", 12))
            style.configure("TCombobox", font=("default", 12))
        except tk.TclError:
            pass
        plt.rcParams.update(
            {
                "font.size": 10,
                "axes.titlesize": 11,
                "axes.labelsize": 10,
                "legend.fontsize": 9,
            }
        )

    # debug helper
    def debug(self, message: str):
        print(f"[RT-BDS {datetime.now().isoformat(timespec='seconds')}] {message}")

    # layout 3 main panels
    def build_ui(self):
        outer = tk.PanedWindow(
            self.app_root,
            orient="horizontal",
            sashrelief="raised",
            sashwidth=6,
            bg="#1E1E2E",
        )
        outer.pack(fill="both", expand=True, padx=6, pady=6)

        setup_frame = tk.LabelFrame(
            outer,
            text="Test Setup",
            font=self.section_font,
            padx=10,
            pady=10,
        )
        manage_frame = tk.LabelFrame(
            outer,
            text="Test Management",
            font=self.section_font,
            padx=10,
            pady=10,
        )
        plots_frame = tk.LabelFrame(
            outer,
            text="Data Plots",
            font=self.section_font,
            padx=10,
            pady=10,
        )

        outer.add(setup_frame, minsize=200, width=600)
        outer.add(manage_frame, minsize=200, width=600)
        outer.add(plots_frame, minsize=200, width=350)

        setup_frame.rowconfigure(0, weight=1)
        setup_frame.columnconfigure(0, weight=1)
        manage_frame.rowconfigure(1, weight=1)
        manage_frame.columnconfigure(0, weight=1)
        plots_frame.rowconfigure(1, weight=1)
        plots_frame.columnconfigure(0, weight=1)

        setup_notebook = ttk.Notebook(setup_frame)
        setup_notebook.grid(row=0, column=0, sticky="nsew")

        freq_tab = tk.Frame(setup_notebook, padx=8, pady=8)
        probe_tab = tk.Frame(setup_notebook, padx=8, pady=8)
        device_tab = tk.Frame(setup_notebook, padx=8, pady=8)
        setup_notebook.add(probe_tab, text="Probes")
        setup_notebook.add(freq_tab, text="Frequency")
        setup_notebook.add(device_tab, text="Devices")

        self.build_probe_tab(probe_tab)
        self.build_frequency_tab(freq_tab)
        self.build_devices_tab(device_tab)
        self.build_management_panel(manage_frame)
        self.build_plots_panel(plots_frame)

    # to allow controls to be shown at all times, setup changes depending on width
    def arrange_frequency_logspace_controls(self, event=None):
        if not hasattr(self, "frequency_logspace_items"):
            return

        width = event.width if event is not None else self.frequency_logspace_box.winfo_width()
        low_freq_frame, high_freq_frame, points_frame = self.frequency_logspace_items
        for item in self.frequency_logspace_items:
            item.grid_forget()
        for column in range(3):
            self.frequency_logspace_box.columnconfigure(column, weight=0)

        if width < 340:
            low_freq_frame.grid(row=0, column=0, sticky="ew", pady=2)
            high_freq_frame.grid(row=1, column=0, sticky="ew", pady=2)
            points_frame.grid(row=2, column=0, sticky="ew", pady=2)
            self.frequency_logspace_box.columnconfigure(0, weight=1)
        elif width < 520:
            low_freq_frame.grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=2)
            high_freq_frame.grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=2)
            points_frame.grid(row=1, column=0, columnspan=2, sticky="w", pady=2)
            self.frequency_logspace_box.columnconfigure(0, weight=1)
            self.frequency_logspace_box.columnconfigure(1, weight=1)
        else:
            low_freq_frame.grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=2)
            high_freq_frame.grid(row=0, column=1, sticky="ew", padx=4, pady=2)
            points_frame.grid(row=0, column=2, sticky="ew", padx=(4, 0), pady=2)
            for column in range(3):
                self.frequency_logspace_box.columnconfigure(column, weight=1)

    # frequency tab w logspace and manual controls
    def build_frequency_tab(self, master):
        sweep_params_box = tk.LabelFrame(
            master=master,
            text="Primary Frequencies: Sweep",
            font=("default", 11),
            padx=10,
            pady=10,
        )
        sweep_params_box.pack(side="top", fill="x")

        frequency_logspace_box = tk.Frame(master=sweep_params_box)
        frequency_logspace_box.pack(side="top", fill="x")
        self.frequency_logspace_box = frequency_logspace_box

        low_freq_frame = tk.Frame(master=frequency_logspace_box)
        low_freq_frame.grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=2)
        tk.Label(master=low_freq_frame, text="First [Hz] ").pack(side="left", fill="y")
        self.low_freq_entry = Entry(
            master=low_freq_frame,
            width=10,
            justify="right",
            textvariable=self.first_freq_strvar,
        )
        self.low_freq_entry.pack(side="left", fill="y")

        high_freq_frame = tk.Frame(master=frequency_logspace_box)
        high_freq_frame.grid(row=0, column=1, sticky="ew", padx=4, pady=2)
        tk.Label(master=high_freq_frame, text=" to Last [Hz] ").pack(
            side="left", fill="y"
        )
        self.high_freq_entry = Entry(
            master=high_freq_frame,
            width=10,
            justify="right",
            textvariable=self.last_freq_strvar,
        )
        self.high_freq_entry.pack(side="left", fill="y")

        points_frame = tk.Frame(master=frequency_logspace_box)
        points_frame.grid(row=0, column=2, sticky="ew", padx=(4, 0), pady=2)
        tk.Label(master=points_frame, text=" @ Points/Dec. ").pack(
            side="left", fill="y"
        )
        self.points_per_decade_entry = Entry(
            master=points_frame,
            width=7,
            justify="right",
            textvariable=self.points_per_decade_strvar,
        )
        self.points_per_decade_entry.pack(side="left", fill="y")
        self.frequency_logspace_items = (
            low_freq_frame,
            high_freq_frame,
            points_frame,
        )
        frequency_logspace_box.bind(
            "<Configure>", self.arrange_frequency_logspace_controls
        )
        self.ui_after(0, self.arrange_frequency_logspace_controls)

        tk.Frame(master=sweep_params_box, height=10).pack(side="top", fill="x")
        logspace_button_box = tk.Frame(master=sweep_params_box)
        logspace_button_box.pack(side="top", fill="x")
        self.set_logspace_button = tk.Button(
            master=logspace_button_box,
            text="Set Logspace",
            command=self.generate_frequency_plan,
        )
        self.set_logspace_button.pack(side="top")
        self.points_per_decade_entry.bind(
            "<Return>", lambda *args: self.set_logspace_button.invoke()
        )

        tk.Frame(master=master, height=10).pack(side="top", fill="x")
        both_freq_table_frame = tk.Frame(master=master)
        both_freq_table_frame.pack(side="top", fill="both", expand=True)

        left_side_box = tk.LabelFrame(
            master=both_freq_table_frame,
            text="Secondary Frequencies",
            font=("default", 11),
            padx=10,
            pady=10,
        )
        left_side_box.pack(side="left", fill="both", expand=True)
        tk.Frame(master=both_freq_table_frame, width=10).pack(side="left")
        right_side_box = tk.LabelFrame(
            master=both_freq_table_frame,
            text="Frequency Steps",
            font=("default", 11),
            padx=10,
            pady=10,
        )
        right_side_box.pack(side="left", fill="both", expand=True)

        setting_buttons_box = tk.Frame(master=left_side_box)
        setting_buttons_box.pack(side="top", fill="x")
        manual_freq_frame = tk.Frame(master=setting_buttons_box)
        manual_freq_frame.pack(side="top", fill="x")
        tk.Label(master=manual_freq_frame, text="Manual [Hz] ").pack(side="left")
        self.manual_freq_entry = Entry(
            master=manual_freq_frame,
            width=10,
            justify="right",
            textvariable=self.manual_freq_strvar,
        )
        self.manual_freq_entry.pack(side="left")

        button_row = tk.Frame(master=setting_buttons_box, padx=10, pady=10)
        button_row.pack(side="top", fill="x", expand=True)
        self.add_freq_button = tk.Button(
            master=button_row,
            text="Add Step",
            command=self.add_manual_freq,
        )
        self.add_freq_button.pack(side="top", fill="x", pady=(0, 4))
        self.remove_freq_button = tk.Button(
            master=button_row,
            text="Drop Step",
            command=self.drop_selected_manual_freq,
        )
        self.remove_freq_button.pack(side="top", fill="x")
        self.clear_freq_button = tk.Button(
            master=setting_buttons_box,
            text="Clear List",
            command=self.clear_manual_freqs,
        )
        self.clear_freq_button.pack(side="top", fill="x", padx=10, pady=(0, 4))
        self.manual_freq_entry.bind(
            "<Return>", lambda *args: self.add_freq_button.invoke()
        )
        self.manual_freq_entry.bind(
            "<Shift-Return>", lambda *args: self.remove_freq_button.invoke()
        )

        self.setup_controls.extend(
            [
                self.low_freq_entry,
                self.high_freq_entry,
                self.points_per_decade_entry,
                self.set_logspace_button,
                self.manual_freq_entry,
                self.add_freq_button,
                self.remove_freq_button,
                self.clear_freq_button,
            ]
        )

        tk.Frame(master=left_side_box, height=10).pack(side="top", fill="x")
        custom_table_with_scroll_frame = tk.Frame(master=left_side_box)
        custom_table_with_scroll_frame.pack(side="top", fill="both", expand=True)
        self.custom_freq_table = Table(
            (60, 120, 28),
            custom_table_with_scroll_frame,
            columns=FREQ_STEP_COLUMNS,
            show="headings",
            height=18,
            selectmode="none",
        )
        self.custom_freq_table.pack(side="left", fill="both", expand=True)
        custom_table_scrollbar = ttk.Scrollbar(
            master=custom_table_with_scroll_frame,
            orient="vertical",
            command=self.custom_freq_table.yview,
        )
        custom_table_scrollbar.pack(side="left", fill="y")
        self.custom_freq_table.configure(yscrollcommand=custom_table_scrollbar.set)

        full_table_with_scroll_frame = tk.Frame(master=right_side_box)
        full_table_with_scroll_frame.pack(side="left", fill="both", expand=True)
        self.freq_step_table = Table(
            (60, 120, 28),
            full_table_with_scroll_frame,
            columns=FREQ_STEP_COLUMNS,
            show="headings",
            height=18,
            selectmode="none",
        )
        self.freq_step_table.pack(side="left", fill="both", expand=True)
        full_table_scrollbar = ttk.Scrollbar(
            master=full_table_with_scroll_frame,
            orient="vertical",
            command=self.freq_step_table.yview,
        )
        full_table_scrollbar.pack(side="left", fill="y")
        self.freq_step_table.configure(yscrollcommand=full_table_scrollbar.set)
        self.freq_table = self.freq_step_table

    def make_vertical_scroll_frame(self, master):
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)

        canvas = tk.Canvas(master, highlightthickness=0)
        scrollbar = ttk.Scrollbar(master, orient="vertical", command=canvas.yview)
        content = ttk.Frame(canvas)
        content_window = canvas.create_window((0, 0), window=content, anchor="nw")

        def update_scroll_region(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def match_content_width(event):
            canvas.itemconfigure(content_window, width=event.width)

        content.bind("<Configure>", update_scroll_region)
        canvas.bind("<Configure>", match_content_width)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        return content

    def toggle_manual_probe_testing(self):
        if not hasattr(self, "manual_probe_testing_frame"):
            return
        visible = getattr(self, "manual_probe_testing_visible", False)
        if visible:
            self.manual_probe_testing_frame.grid_remove()
            self.manual_probe_testing_button.configure(text="Show Manual Probe Testing")
            self.manual_probe_testing_visible = False
        else:
            self.manual_probe_testing_frame.grid()
            self.manual_probe_testing_button.configure(text="Hide Manual Probe Testing")
            self.manual_probe_testing_visible = True

    # probe setup tab, relay assignment and switch control
    def build_probe_tab(self, master):
        content = self.make_vertical_scroll_frame(master)
        content.columnconfigure(0, weight=1)

        settings_frame = ttk.LabelFrame(content, text="Probe Settings", padding=6)
        settings_frame.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.add_labeled_entry(
            settings_frame,
            "Settling delay [s]",
            self.probe_settling_strvar,
            row=0,
            controls=self.setup_controls,
        )
        self.add_labeled_entry(
            settings_frame,
            "Film area [mm^2]",
            self.film_area_mm2_strvar,
            row=1,
            controls=self.setup_controls,
        )
        self.add_labeled_entry(
            settings_frame,
            "Film thickness [um]",
            self.film_thickness_um_strvar,
            row=2,
            controls=self.setup_controls,
        )

        probe_grid_box = ttk.LabelFrame(
            content, text="Probe Enable / Relay Setup", padding=6
        )
        probe_grid_box.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        probe_grid_box.columnconfigure(0, weight=1)
        probe_grid_frame = ttk.Frame(probe_grid_box)
        probe_grid_frame.grid(row=0, column=0, sticky="ew")
        for column in (2, 3):
            probe_grid_frame.columnconfigure(column, weight=1)

        headings = ("Enabled", "Probe", "Label", "Relay")
        for column, heading in enumerate(headings):
            ttk.Label(probe_grid_frame, text=heading).grid(
                row=0, column=column, sticky="w", padx=2, pady=2
            )

        for row_index, probe_vars in enumerate(self.probe_vars, start=1):
            enabled = ttk.Checkbutton(
                probe_grid_frame, variable=probe_vars["enabled"]
            )
            probe_label = ttk.Label(probe_grid_frame, text=f"{row_index:02d}")
            label_entry = Entry(probe_grid_frame, textvariable=probe_vars["label"])
            relay_entry = Entry(probe_grid_frame, textvariable=probe_vars["relay"])

            enabled.grid(row=row_index, column=0, sticky="w", padx=2, pady=1)
            probe_label.grid(row=row_index, column=1, sticky="w", padx=2, pady=1)
            label_entry.grid(row=row_index, column=2, sticky="ew", padx=2, pady=1)
            relay_entry.grid(row=row_index, column=3, sticky="ew", padx=2, pady=1)
            self.setup_controls.extend([enabled, label_entry, relay_entry])

        table_frame = ttk.LabelFrame(content, text="Enabled Probe Table", padding=6)
        table_frame.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.probe_table = Table(
            (70, 80, 150, 80),
            table_frame,
            columns=PROBE_TABLE_COLUMNS,
            show="headings",
            height=5,
        )
        probe_scroll = ttk.Scrollbar(
            table_frame, orient="vertical", command=self.probe_table.yview
        )
        self.probe_table.configure(yscrollcommand=probe_scroll.set)
        self.probe_table.grid(row=0, column=0, sticky="nsew")
        probe_scroll.grid(row=0, column=1, sticky="ns")

        testing_section = ttk.LabelFrame(content, text="Testing", padding=6)
        testing_section.grid(row=3, column=0, sticky="ew")
        testing_section.columnconfigure(0, weight=1)
        self.manual_probe_testing_button = ttk.Button(
            testing_section,
            text="Show Manual Probe Testing",
            command=self.toggle_manual_probe_testing,
        )
        self.manual_probe_testing_button.grid(row=0, column=0, sticky="ew")
        self.manual_probe_controls.append(self.manual_probe_testing_button)

        manual_frame = ttk.Frame(testing_section)
        manual_frame.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.manual_probe_testing_frame = manual_frame
        self.manual_probe_testing_visible = True
        manual_columns = 2
        for column in range(manual_columns):
            manual_frame.columnconfigure(column, weight=1, minsize=84)
        for probe_index in range(1, MAX_PROBES + 1):
            button = ttk.Button(
                manual_frame,
                text=f"Probe {probe_index}",
                command=lambda p=probe_index: self.manual_select_probe(p),
                width=9,
            )
            button.grid(
                row=(probe_index - 1) // manual_columns,
                column=(probe_index - 1) % manual_columns,
                sticky="ew",
                padx=2,
                pady=2,
            )
            self.manual_probe_controls.append(button)

        all_off_button = ttk.Button(
            manual_frame, text="All Off", command=self.manual_relay_all_off
        )
        read_status_button = ttk.Button(
            manual_frame, text="Read Status", command=self.manual_read_relay_status
        )
        action_row = (MAX_PROBES + manual_columns - 1) // manual_columns
        all_off_button.grid(
            row=action_row, column=0, sticky="ew", padx=2, pady=4
        )
        read_status_button.grid(
            row=action_row, column=1, sticky="ew", padx=2, pady=4
        )
        status_label = ttk.Label(
            manual_frame, textvariable=self.relay_status_strvar, wraplength=360
        )
        status_label.grid(
            row=action_row + 1,
            column=0,
            columnspan=manual_columns,
            sticky="ew",
            padx=2,
        )
        self.manual_probe_controls.extend([all_off_button, read_status_button])
        self.toggle_manual_probe_testing()

    # devices tab for lcr n relay
    def build_devices_tab(self, master):
        master.columnconfigure(1, weight=1)

        ttk.Label(master, text="Device").grid(row=0, column=0, sticky="w", pady=2)
        self.device_combo = ttk.Combobox(
            master,
            textvariable=self.device_strvar,
            values=DEVICE_NAMES,
            state="readonly",
        )
        self.device_combo.grid(row=0, column=1, sticky="ew", pady=2)
        self.manual_device_controls.append(self.device_combo)

        connect_button = ttk.Button(
            master, text="Connect Selected", command=self.connect_selected_device
        )
        connect_button.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 8))
        self.manual_device_controls.append(connect_button)

        ttk.Label(master, text="Command").grid(row=2, column=0, sticky="w", pady=2)
        message_entry = Entry(master, textvariable=self.message_strvar)
        message_entry.grid(row=2, column=1, sticky="ew", pady=2)
        send_button = ttk.Button(master, text="Send", command=self.device_msg)
        send_button.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 8))
        self.manual_device_controls.extend([message_entry, send_button])

        response = ttk.Label(
            master,
            textvariable=self.response_strvar,
            anchor="nw",
            justify="left",
            wraplength=360,
        )
        response.grid(row=4, column=0, columnspan=2, sticky="nsew")

    def arrange_management_controls(self, event=None):
        if not hasattr(self, "management_control_boxes"):
            return

        width = (
            event.width
            if event is not None
            else self.management_controls_frame.winfo_width()
        )
        test_control_box, action_box = self.management_control_boxes
        layout = "stack" if width < 280 else "row"
        if layout == getattr(self, "management_controls_layout", None):
            return
        self.management_controls_layout = layout

        for box in self.management_control_boxes:
            box.grid_forget()
        self.management_controls_frame.columnconfigure(0, weight=1, minsize=170)
        test_control_box.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        action_box.grid(row=1, column=0, sticky="ew")
        self.export_button.grid_forget()
        self.new_run_button.grid_forget()

        if layout == "stack":
            action_box.columnconfigure(0, weight=1, minsize=120)
            action_box.columnconfigure(1, weight=0, minsize=0)
            self.export_button.grid(row=0, column=0, sticky="ew", pady=(0, 4))
            self.new_run_button.grid(row=1, column=0, sticky="ew")
        else:
            action_box.columnconfigure(0, weight=1, minsize=90)
            action_box.columnconfigure(1, weight=1, minsize=90)
            self.export_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
            self.new_run_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))

    # run control, status, and measurement table
    def build_management_panel(self, master):
        master.columnconfigure(0, weight=1)
        master.rowconfigure(1, weight=1)

        controls_frame = ttk.Frame(master)
        controls_frame.grid(row=0, column=0, sticky="ew")
        self.management_controls_frame = controls_frame
        controls_frame.columnconfigure(0, weight=1)

        test_control_box = tk.LabelFrame(
            controls_frame,
            text="Test Control",
            font=("default", 12, "bold"),
            padx=8,
            pady=8,
        )
        test_control_box.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        test_control_box.columnconfigure(0, weight=1)
        test_control_box.columnconfigure(1, weight=1)
        test_control_box.columnconfigure(2, weight=2)

        self.run_button = tk.Button(
            test_control_box,
            text="Run",
            command=self.start_run,
            bg="#cfead6",
            activebackground="#b8dfc3",
            fg="#173f25",
            relief="raised",
            bd=2,
            width=9,
        )
        self.stop_button = tk.Button(
            test_control_box,
            text="Stop",
            command=self.request_stop,
            bg="#f3c7c2",
            activebackground="#ebb0aa",
            fg="#5d1f19",
            relief="raised",
            bd=2,
            width=9,
        )
        self.run_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        status_box = tk.Frame(test_control_box, bd=1, relief="solid", padx=6, pady=4)
        status_box.grid(
            row=0, column=2, rowspan=2, sticky="nsew", padx=(8, 0)
        )
        status_box.columnconfigure(0, weight=1)
        tk.Label(
            status_box,
            textvariable=self.status_strvar,
            anchor="w",
            bg="#f6f8fa",
        ).grid(row=0, column=0, sticky="ew")
        self.progress_bar = ttk.Progressbar(
            status_box,
            variable=self.progress_value,
            maximum=100.0,
            mode="determinate",
        )
        self.progress_bar.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(
            status_box,
            textvariable=self.progress_strvar,
            anchor="w",
        ).grid(row=2, column=0, sticky="ew", pady=(2, 0))

        action_box = tk.LabelFrame(
            controls_frame,
            text="Data Actions",
            font=("default", 12, "bold"),
            padx=8,
            pady=8,
        )
        action_box.grid(row=1, column=0, sticky="ew")
        action_box.columnconfigure(0, weight=1)
        action_box.columnconfigure(1, weight=1)
        self.export_button = tk.Button(
            action_box,
            text="Export",
            command=self.export_results,
            width=10,
            bg="#d9e8f5",
            activebackground="#c4dcef",
            relief="raised",
            bd=2,
        )
        self.new_run_button = tk.Button(
            action_box,
            text="New Run",
            command=self.new_run,
            width=10,
            relief="raised",
            bd=2,
        )
        self.export_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.new_run_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self.idle_controls.extend([self.new_run_button, self.export_button])
        self.management_control_boxes = (
            test_control_box,
            action_box,
        )
        self.management_controls_layout = None
        controls_frame.bind("<Configure>", self.arrange_management_controls)
        self.ui_after(0, self.arrange_management_controls)

        table_frame = ttk.Frame(master)
        table_frame.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.measurement_table = Table(
            (145, 80, 120, 80, 95, 95, 130, 85, 95, 140),
            table_frame,
            columns=TEST_DATA_COLUMNS,
            show="headings",
            height=MEASUREMENT_DISPLAY_ROWS,
        )
        y_scroll = ttk.Scrollbar(
            table_frame, orient="vertical", command=self.measurement_table.yview
        )
        x_scroll = ttk.Scrollbar(
            table_frame, orient="horizontal", command=self.measurement_table.xview
        )
        self.measurement_table.configure(
            yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set
        )
        self.measurement_table.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")

    # plots panel with notebooks
    def build_plots_panel(self, master):
        controls_frame = ttk.Frame(master)
        controls_frame.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        controls_frame.columnconfigure(1, weight=1)
        ttk.Label(controls_frame, text="Selected frequency [Hz]").grid(
            row=0, column=0, sticky="w", padx=(0, 4)
        )
        self.plot_freq_combo = ttk.Combobox(
            controls_frame,
            textvariable=self.selected_plot_freq_strvar,
            values=[],
            state="readonly",
        )
        self.plot_freq_combo.grid(row=0, column=1, sticky="ew")
        self.plot_freq_combo.bind("<<ComboboxSelected>>", lambda *_: self.update_plots())
        ttk.Label(controls_frame, text="Probe comparison").grid(
            row=1, column=0, sticky="w", padx=(0, 4), pady=(4, 0)
        )
        self.probe_select_button = tk.Menubutton(
            controls_frame,
            text="Probe 1",
            relief="raised",
            anchor="w",
        )
        self.probe_select_menu = tk.Menu(self.probe_select_button, tearoff=False)
        self.probe_select_button.configure(menu=self.probe_select_menu)
        self.probe_select_button.grid(row=1, column=1, sticky="ew", pady=(4, 0))
        self.build_probe_selector_menu()

        plot_notebook = ttk.Notebook(master)
        plot_notebook.grid(row=1, column=0, sticky="nsew")

        versus_tab = ttk.Frame(plot_notebook)
        probe_tab = ttk.Frame(plot_notebook)
        dielectric_tab = ttk.Frame(plot_notebook)
        plot_notebook.add(versus_tab, text="Versus Frequency")
        plot_notebook.add(probe_tab, text="Probe Comparison")
        plot_notebook.add(dielectric_tab, text="Dielectric Constant")

        self.freq_fig, self.freq_axes = plt.subplots(3, 1, figsize=(6.8, 6.4))
        self.probe_fig, self.probe_axes = plt.subplots(3, 1, figsize=(6.8, 6.4))
        self.dielectric_fig, self.dielectric_axis = plt.subplots(
            1, 1, figsize=(6.8, 4.8)
        )
        self.freq_fig.tight_layout(pad=2.0)
        self.probe_fig.tight_layout(pad=2.0)
        self.dielectric_fig.tight_layout(pad=2.0)

        self.freq_canvas = FigureCanvasTkAgg(self.freq_fig, master=versus_tab)
        self.freq_canvas.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(self.freq_canvas, versus_tab).update()

        self.probe_canvas = FigureCanvasTkAgg(self.probe_fig, master=probe_tab)
        self.probe_canvas.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(self.probe_canvas, probe_tab).update()

        self.dielectric_canvas = FigureCanvasTkAgg(
            self.dielectric_fig, master=dielectric_tab
        )
        self.dielectric_canvas.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(self.dielectric_canvas, dielectric_tab).update()

    # refresh probe selection for probe comparison
    def build_probe_selector_menu(self):
        if not hasattr(self, "probe_select_menu"):
            return
        self.probe_select_menu.delete(0, "end")
        for probe_index, var in enumerate(self.probe_plot_vars, start=1):
            label = self.probe_vars[probe_index - 1]["label"].get().strip()
            if not label:
                label = f"Probe {probe_index}"
            self.probe_select_menu.add_checkbutton(
                label=f"{probe_index:02d} - {label}",
                variable=var,
                command=self.on_probe_plot_selection_changed,
            )
        self.update_probe_selector_label()

    # refresh plots if probe selection changes
    def on_probe_plot_selection_changed(self):
        if not self.get_selected_plot_probe_indices():
            self.probe_plot_vars[0].set(True)
        self.update_probe_selector_label()
        self.update_plots()

    # selected index getter
    def get_selected_plot_probe_indices(self) -> list[int]:
        return [
            probe_index
            for probe_index, var in enumerate(self.probe_plot_vars, start=1)
            if bool(var.get())
        ]

    # update label
    def update_probe_selector_label(self):
        if not hasattr(self, "probe_select_button"):
            return
        selected = self.get_selected_plot_probe_indices()
        if not selected:
            text = "Probe 1"
        elif len(selected) == 1:
            text = f"Probe {selected[0]}"
        else:
            text = f"{len(selected)} probes"
        self.probe_select_button.configure(text=text)

    # label parameters
    def add_labeled_entry(self, master, label, variable, row, controls):
        ttk.Label(master, text=label).grid(row=row, column=0, sticky="w", pady=2)
        entry = Entry(master, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", pady=2)
        master.columnconfigure(1, weight=1)
        controls.append(entry)
        return entry

    # positive float return from user
    def parse_positive_float(self, value: str, label: str) -> float:
        try:
            parsed = float(value)
        except ValueError as exc:
            raise ValueError(f"{label} must be numeric.") from exc
        if parsed <= 0:
            raise ValueError(f"{label} must be greater than zero.")
        return parsed

    def generate_frequency_plan(self):
        try:
            first = self.parse_positive_float(
                self.first_freq_strvar.get(), "First frequency"
            )
            last = self.parse_positive_float(
                self.last_freq_strvar.get(), "Last frequency"
            )
            points_per_decade = int(float(self.points_per_decade_strvar.get()))
            if points_per_decade <= 0:
                raise ValueError("Points per decade must be greater than zero.")

            self.validate_frequency_list([first, last])
            if first >= last:
                raise ValueError("First frequency must be less than last frequency.")

            decades = np.log10(last) - np.log10(first)
            count = max(2, int(np.floor(decades * points_per_decade)) + 1)
            freqs = np.logspace(np.log10(first), np.log10(last), count)

            freqs = sorted({round(float(freq), 9) for freq in freqs})
            self.freq_step_data = pd.DataFrame(
                [
                    (index, freq, "")
                    for index, freq in enumerate(freqs, start=1)
                ],
                columns=FREQ_STEP_COLUMNS,
            )
            self.refresh_frequency_table()
            self.update_plot_filter_options()
            self.debug(f"Frequency logspace list generated with {len(freqs)} point(s).")
        except Exception as e:
            messagebox.showerror("Frequency Plan", str(e))
            self.debug(f"Frequency list generation failed: {e}")

    # manual additions
    def add_manual_freq(self):
        try:
            freq = self.parse_positive_float(
                self.manual_freq_strvar.get(), "Manual frequency"
            )
            self.validate_frequency_list([freq])
            current = self.custom_freq_data.copy()
            next_step = len(current) + 1
            existing = self.get_frequency_dataframe()
            existing_freqs = pd.to_numeric(
                existing.get(FREQ_STEP_COLUMNS[1], pd.Series(dtype=float)),
                errors="coerce",
            )
            if any(np.isclose(existing_freqs.dropna(), freq, rtol=1e-9, atol=1e-9)):
                messagebox.showwarning(
                    "Duplicate Frequency",
                    f"{freq:g} Hz already exists in the frequency list.",
                )
                return
            new_row = pd.DataFrame(
                [(next_step, round(freq, 9), "*")],
                columns=FREQ_STEP_COLUMNS,
            )
            current = pd.concat([current, new_row], ignore_index=True)
            current = current.drop_duplicates(
                subset=[FREQ_STEP_COLUMNS[1]], keep="last"
            ).reset_index(drop=True)
            current[FREQ_STEP_COLUMNS[0]] = range(1, len(current) + 1)
            self.custom_freq_data = current
            self.refresh_frequency_table()
            self.update_plot_filter_options()
            self.debug(f"Manual frequency added: {freq:g} Hz.")
        except Exception as e:
            messagebox.showerror("Manual Frequency", str(e))
            self.debug(f"Manual frequency add failed: {e}")

    def drop_selected_manual_freq(self):
        current = self.custom_freq_data.copy()
        if current.empty:
            return
        current = current.drop(current.tail(1).index)
        current = current.reset_index(drop=True)
        current[FREQ_STEP_COLUMNS[0]] = range(1, len(current) + 1)
        self.custom_freq_data = current
        self.refresh_frequency_table()
        self.update_plot_filter_options()
        self.debug("Most recent manual frequency dropped.")

    def clear_manual_freqs(self):
        self.custom_freq_data = pd.DataFrame(columns=FREQ_STEP_COLUMNS)
        self.refresh_frequency_table()
        self.update_plot_filter_options()
        self.debug("Manual frequency list cleared.")

    def refresh_frequency_table(self):
        combined = self.get_frequency_dataframe()
        if hasattr(self, "custom_freq_table"):
            self.custom_freq_table.update_table(self.custom_freq_data)
        if hasattr(self, "freq_step_table"):
            self.freq_step_table.update_table(combined)
        elif hasattr(self, "freq_table"):
            self.freq_table.update_table(combined)

    def get_frequency_dataframe(self) -> pd.DataFrame:
        combined = pd.concat(
            [self.freq_step_data, self.custom_freq_data], ignore_index=True
        )
        if combined.empty:
            return pd.DataFrame(columns=FREQ_STEP_COLUMNS)

        combined[FREQ_STEP_COLUMNS[1]] = pd.to_numeric(
            combined[FREQ_STEP_COLUMNS[1]], errors="coerce"
        )
        combined = combined.dropna(subset=[FREQ_STEP_COLUMNS[1]])
        combined = combined.drop_duplicates(
            subset=[FREQ_STEP_COLUMNS[1]], keep="last"
        ).sort_values(FREQ_STEP_COLUMNS[1])
        combined = combined.reset_index(drop=True)
        combined[FREQ_STEP_COLUMNS[0]] = range(1, len(combined) + 1)
        return combined.loc[:, FREQ_STEP_COLUMNS]

    def get_frequency_list(self):
        combined = self.get_frequency_dataframe()
        if combined.empty:
            return []

        freqs = (
            combined[FREQ_STEP_COLUMNS[1]]
            .dropna()
            .astype(float)
            .drop_duplicates()
            .sort_values()
            .tolist()
        )
        return freqs

    # freq val validation
    def validate_frequency_list(self, freqs):
        if not freqs:
            raise ValueError("Frequency list is empty.")

        clean_freqs = []
        for freq in freqs:
            try:
                clean = float(freq)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid frequency value: {freq!r}") from exc
            if not np.isfinite(clean):
                raise ValueError(f"Invalid frequency value: {freq!r}")
            if clean < devices.LCR_MIN_FREQ or clean > devices.LCR_MAX_FREQ:
                raise ValueError(
                    f"Frequency {clean:g} Hz is outside the LCR range "
                    f"{devices.LCR_MIN_FREQ:g} to {devices.LCR_MAX_FREQ:g} Hz."
                )
            clean_freqs.append(clean)

        return sorted(set(clean_freqs))

    def refresh_probe_table(self):
        if not hasattr(self, "probe_table"):
            return

        rows = []
        for probe_index, probe_vars in enumerate(self.probe_vars, start=1):
            if bool(probe_vars["enabled"].get()):
                rows.append(
                    (
                        "Yes",
                        probe_index,
                        probe_vars["label"].get().strip() or f"Probe {probe_index:02d}",
                        probe_vars["relay"].get().strip(),
                    )
                )

        self.probe_table.update_table(
            pd.DataFrame(rows, columns=PROBE_TABLE_COLUMNS)
        )

    # refresh on change
    def on_probe_setup_changed(self):
        self.refresh_probe_table()
        if hasattr(self, "probe_select_menu"):
            self.build_probe_selector_menu()

    def get_probe_configs(self, strict: bool = True):
        configs = []
        relay_assignments: dict[int, int] = {}

        for probe_index, probe_vars in enumerate(self.probe_vars, start=1):
            if not bool(probe_vars["enabled"].get()):
                continue

            label = probe_vars["label"].get().strip() or f"Probe {probe_index:02d}"
            relay_text = probe_vars["relay"].get().strip()

            try:
                relay_index = int(relay_text)
            except ValueError as exc:
                if strict:
                    raise ValueError(
                        f"Probe {probe_index} relay index must be an integer."
                    ) from exc
                relay_index = probe_index

            if relay_index < 1 or relay_index > MAX_PROBES:
                raise ValueError(
                    f"Probe {probe_index} relay index must be between 1 and 16."
                )

            if relay_index in relay_assignments:
                other_probe = relay_assignments[relay_index]
                raise ValueError(
                    f"Relay {relay_index} is assigned to both probe "
                    f"{other_probe} and probe {probe_index}."
                )

            relay_assignments[relay_index] = probe_index
            configs.append((probe_index, label, relay_index))

        if strict and not configs:
            raise ValueError("Enable at least one probe.")

        return configs

    def get_probe_settling_delay(self) -> float:
        try:
            delay = float(self.probe_settling_strvar.get())
        except ValueError as exc:
            raise ValueError("Settling delay must be numeric.") from exc
        if delay < 0:
            raise ValueError("Settling delay must be nonnegative.")
        return delay

    def parse_optional_positive_float(self, value: str, label: str) -> float:
        value = value.strip()
        if not value:
            return np.nan
        parsed = self.parse_positive_float(value, label)
        if not np.isfinite(parsed):
            raise ValueError(f"{label} must be finite.")
        return parsed

    # K calculated only if dimensions r provided
    def get_film_geometry(self) -> tuple[float, float]:
        area_text = self.film_area_mm2_strvar.get().strip()
        thickness_text = self.film_thickness_um_strvar.get().strip()
        if not area_text and not thickness_text:
            return np.nan, np.nan
        if not area_text or not thickness_text:
            raise ValueError(
                "Enter both film area and film thickness, or leave both blank."
            )
        area_mm2 = self.parse_optional_positive_float(area_text, "Film area")
        thickness_um = self.parse_optional_positive_float(
            thickness_text, "Film thickness"
        )
        return area_mm2, thickness_um

    def get_traceability_values(self) -> tuple[str, str]:
        return self.roll_id_strvar.get().strip(), self.operator_strvar.get().strip()

    def ensure_traceability_metadata(self) -> bool:
        if self.traceability_confirmed:
            return True
        return self.prompt_traceability_metadata()

    def prompt_traceability_metadata(self) -> bool:
        result = {"confirmed": False}
        dialog = tk.Toplevel(self.app_root)
        dialog.title("Run Traceability")
        dialog.transient(self.app_root)
        dialog.resizable(False, False)
        dialog.grab_set()

        roll_id_var = tk.StringVar(value=self.roll_id_strvar.get())
        operator_var = tk.StringVar(value=self.operator_strvar.get())

        body = ttk.Frame(dialog, padding=12)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="Roll ID").grid(row=0, column=0, sticky="w", pady=4)
        roll_entry = Entry(body, textvariable=roll_id_var, width=30)
        roll_entry.grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=4)

        ttk.Label(body, text="Operator").grid(row=1, column=0, sticky="w", pady=4)
        operator_entry = Entry(body, textvariable=operator_var, width=30)
        operator_entry.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=4)

        button_frame = ttk.Frame(body)
        button_frame.grid(row=2, column=0, columnspan=2, sticky="e", pady=(10, 0))

        def accept():
            self.roll_id_strvar.set(roll_id_var.get().strip())
            self.operator_strvar.set(operator_var.get().strip())
            self.traceability_confirmed = True
            result["confirmed"] = True
            dialog.destroy()

        def cancel():
            result["confirmed"] = False
            dialog.destroy()

        ttk.Button(button_frame, text="Cancel", command=cancel).pack(
            side="right", padx=(6, 0)
        )
        ttk.Button(button_frame, text="OK", command=accept).pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.bind("<Return>", lambda *_: accept())
        dialog.bind("<Escape>", lambda *_: cancel())

        if not roll_id_var.get().strip():
            roll_entry.focus_set()
        else:
            operator_entry.focus_set()
        self.app_root.wait_window(dialog)
        return result["confirmed"]

    # C = ekA/d -> k = Cd/Ae -  C =capacitance, e=permittivity, A=area, d=thickness
    def calculate_dielectric_constant(
        self,
        capacitance_f: float,
        film_area_mm2: float,
        film_thickness_um: float,
    ) -> float:
        if not all(
            np.isfinite(value)
            for value in (capacitance_f, film_area_mm2, film_thickness_um)
        ):
            return np.nan
        area_m2 = film_area_mm2 * 1e-6
        thickness_m = film_thickness_um * 1e-6
        if area_m2 <= 0 or thickness_m <= 0:
            return np.nan
        return capacitance_f * thickness_m / (EPSILON_0_F_PER_M * area_m2)

    def get_run_data(self, *args):
        film_area_mm2, film_thickness_um = self.get_film_geometry()
        return RunConfig(
            probe_configs=tuple(self.get_probe_configs(strict=True)),
            probe_settling_delay=self.get_probe_settling_delay(),
            film_area_mm2=film_area_mm2,
            film_thickness_um=film_thickness_um,
        )

    def connect_selected_device(self):
        try:
            device_type = self.get_selected_device_type()
            device = self.ensure_device_connected(device_type)
            self.response_strvar.set(f"Connected: {device.name}\n{device.address}")
            self.debug(f"Connected device: {device.name} at {device.address}.")
        except Exception as e:
            messagebox.showerror(
                "Device Connection", f"{type(e).__name__}: {e}"
            )

    def get_selected_device_type(self):
        selected_name = self.device_strvar.get()
        for device_type in devices.DEVICE_TYPE_LIST:
            if device_type.name == selected_name:
                return device_type
        raise RuntimeError("Select a device.")

    def ensure_device_connected(self, device_type):
        device = self.get_device_by_type(device_type)
        if device is not None:
            return device

        device = device_type()
        self.device_list.append(device)
        return device

    def get_device_by_type(self, device_type):
        for device in self.device_list:
            if isinstance(device, device_type):
                return device
        return None

    def device_msg(
        self,
        query: str = "",
        device=None,
        hushed=False,
        read_after_write=False,
    ):
        if device is None:
            try:
                device = self.ensure_device_connected(self.get_selected_device_type())
            except Exception as e:
                if not hushed:
                    self.response_strvar.set(f"Error: {type(e).__name__}: {e}")
                return (-1, f"Error: {type(e).__name__}: {e}")

        command = query if query else self.message_strvar.get()
        if not command.strip():
            return (-1, "No command entered.")

        manual_command = not bool(query)
        if manual_command and self.is_sequence_active():
            reply = "Manual device commands are blocked during an active run."
            self.response_strvar.set(reply)
            self.debug(reply)
            return (-1, reply)

        if isinstance(device, devices.DenkoviRelayBoard):
            with self.relay_lock:
                code, reply = device.send(
                    cmd=command, read_after_write=read_after_write
                )
        else:
            with self.hardware_lock:
                code, reply = device.send(
                    cmd=command, read_after_write=read_after_write
                )

        display_text = f"Length: {code} -->\n{reply}"
        self.ui_after(0, lambda: self.response_strvar.set(display_text))

        if not hushed:
            self.debug(f"Command: '{command}' ({code}) -> '{reply}'")

        return (code, reply)

    def checked_device_msg(
        self,
        device,
        query: str,
        context: str = "Device Command",
        read_after_write=False,
    ):
        code, reply = self.device_msg(
            device=device,
            query=query,
            hushed=True,
            read_after_write=read_after_write,
        )
        if code < 0:
            raise RuntimeError(f"{context} failed: {query} -> {reply}")
        return code, reply

    def manual_select_probe(self, probe_index: int):
        if self.is_sequence_active():
            self.debug("Manual relay select blocked during active run.")
            return
        try:
            relay_text = self.probe_vars[probe_index - 1]["relay"].get().strip()
            relay_index = int(relay_text)
            if relay_index < 1 or relay_index > MAX_PROBES:
                raise ValueError("Relay index must be between 1 and 16.")
            relay = self.ensure_device_connected(devices.DenkoviRelayBoard)
            with self.relay_lock:
                relay.select_one(relay_index)
            self.relay_status_strvar.set(
                f"Selected probe {probe_index} on relay {relay_index}."
            )
            self.debug(f"Manual relay select: probe {probe_index}, relay {relay_index}.")
        except Exception as e:
            self.safe_relay_all_off()
            messagebox.showerror("Relay Switch", f"{type(e).__name__}: {e}")

    def manual_relay_all_off(self):
        if self.is_sequence_active():
            self.debug("Manual relay all-off blocked during active run.")
            return
        try:
            relay = self.ensure_device_connected(devices.DenkoviRelayBoard)
            with self.relay_lock:
                relay.all_off()
            self.relay_status_strvar.set("All relays off.")
            self.debug("Manual relay all-off complete.")
        except Exception as e:
            messagebox.showerror("Relay All Off", f"{type(e).__name__}: {e}")

    def manual_read_relay_status(self):
        if self.is_sequence_active():
            self.debug("Manual relay status read blocked during active run.")
            return
        try:
            relay = self.ensure_device_connected(devices.DenkoviRelayBoard)
            with self.relay_lock:
                status = relay.get_status()
            active = [relay_index for relay_index, enabled in status.items() if enabled]
            status_text = f"Status: {status}"
            if active:
                status_text += f"\nActive relays: {active}"
            else:
                status_text += "\nActive relays: none"
            self.relay_status_strvar.set(status_text)
            self.debug(status_text)
        except Exception as e:
            messagebox.showerror("Relay Status", f"{type(e).__name__}: {e}")

    def parse_lcr_reply(self, reply: str):
        parts = str(reply).strip().split(",")
        values = []
        for part in parts:
            try:
                value = float(part.strip())
                if abs(value) >= KEYSIGHT_OVERRANGE_THRESHOLD:
                    value = np.nan
                values.append(value)
            except ValueError:
                pass

        if len(values) >= 2:
            return values[0], values[1]

        return np.nan, np.nan

    def measure_lcr_at_freq(self, lcr, freq_hz: float):
        def checked_send(command):
            if self.stop_is_requested():
                raise InterruptedError("Stop requested before LCR command.")
            code, reply = self.device_msg(device=lcr, query=command, hushed=True)
            if code < 0:
                raise RuntimeError(f"LCR command failed: {command} -> {reply}")
            if self.stop_is_requested():
                raise InterruptedError("Stop requested after LCR command.")
            return reply

        for command in [
            "*CLS",
            ":FUNC:IMP:TYPE CPD",
            f":FREQ:CW {freq_hz}",
            ":FUNC:IMP:RANG:AUTO ON",
            ":TRIG:SOUR BUS",
            ":INIT:CONT OFF",
            ":INIT",
            "*TRG",
            "*WAI",
        ]:
            checked_send(command)
        checked_send("*OPC?")

        cpd_reply = checked_send(":FETC:IMP:FORM?")
        cp, df = self.parse_lcr_reply(cpd_reply)

        for command in [
            ":FUNC:IMP:TYPE CSRS",
            f":FREQ:CW {freq_hz}",
            ":INIT",
            "*TRG",
            "*WAI",
        ]:
            checked_send(command)
        checked_send("*OPC?")

        csrs_reply = checked_send(":FETC:IMP:FORM?")
        _, esr = self.parse_lcr_reply(csrs_reply)

        return cp, df, esr

    def start_run(self):
        if self.is_sequence_active():
            return
        if not self.ensure_traceability_metadata():
            self.debug("Run canceled before traceability metadata was confirmed.")
            return

        try:
            cfg = self.get_run_data()
            freqs = self.validate_frequency_list(self.get_frequency_list())
        except Exception as e:
            messagebox.showerror("Run Setup", str(e))
            return

        self.stop_requested = False
        self.stop_event.clear()
        total_measurements = len(cfg.probe_configs) * len(freqs)
        self.set_run_progress(0, total_measurements, "Starting")
        self.debug(
            f"Run started with {len(cfg.probe_configs)} probe(s) and {len(freqs)} frequency point(s)."
        )
        self.set_state(RUN_STATE.PROBE_SWITCHING, "Starting run...")
        self.run_thread = threading.Thread(
            target=self.run_sequence, args=(cfg, freqs), daemon=True
        )
        self.run_thread.start()
        self.update_control_states()

    def run_sequence(self, cfg: RunConfig, freqs: list[float]):  # on background thread
        relay = None
        final_status = "Done"
        total_measurements = len(cfg.probe_configs) * len(freqs)
        completed_measurements = 0
        try:
            self.set_state(RUN_STATE.PROBE_SWITCHING, "Connecting devices...")
            self.set_run_progress(0, total_measurements, "Connecting devices")
            lcr = self.ensure_device_connected(devices.KeysightLCR_E4980A)
            relay = self.ensure_device_connected(devices.DenkoviRelayBoard)

            with self.relay_lock:
                relay.all_off()  # break before make
            self.append_status("Relay board forced all off before run.")

            for probe_index, label, relay_index in cfg.probe_configs:
                if self.stop_is_requested():
                    final_status = "Stopped"
                    break

                self.set_state(
                    RUN_STATE.PROBE_SWITCHING,
                    f"Switching to {label} on relay {relay_index}...",
                )
                try:
                    with self.relay_lock:
                        relay.select_one(relay_index)
                except Exception as exc:
                    final_status = "Relay switching failed"
                    self.show_error_threadsafe(
                        "Relay Switching Failed",
                        f"Probe {probe_index} relay {relay_index}: "
                        f"{type(exc).__name__}: {exc}",
                    )
                    break

                if not self.wait_seconds(cfg.probe_settling_delay):
                    final_status = "Stopped"
                    break

                rows = []
                self.set_state(RUN_STATE.LCR_MEASURING, f"Measuring {label}...")
                # check before a measurement and after a measurement
                for freq_hz in freqs:
                    if self.stop_is_requested():
                        final_status = "Stopped"
                        break

                    status = "OK"
                    try:
                        cp, df, esr = self.measure_lcr_at_freq(lcr, freq_hz)
                    except Exception as exc:
                        cp, df, esr = np.nan, np.nan, np.nan
                        status = f"ERROR: {type(exc).__name__}: {exc}"
                    if self.stop_is_requested():
                        final_status = "Stopped"
                        break
                    dielectric_constant = self.calculate_dielectric_constant(
                        cp,
                        cfg.film_area_mm2,
                        cfg.film_thickness_um,
                    )

                    rows.append(
                        {
                            "Timestamp": datetime.now().isoformat(timespec="seconds"),
                            "Probe Index": probe_index,
                            "Probe Label": label,
                            "Relay Index": relay_index,
                            "Freq. [Hz]": freq_hz,
                            "Cp [F]": cp,
                            DIELECTRIC_COLUMN: dielectric_constant,
                            "Df [1]": df,
                            f"ESR [{CHAR_OHM}]": esr,
                            "Status": status,
                        }
                    )
                    completed_measurements += 1
                    self.set_run_progress(
                        completed_measurements,
                        total_measurements,
                        f"{label} @ {freq_hz:g} Hz",
                    )

                if rows:
                    self.append_measurement_rows(rows)
                    self.ui_after(0, self.update_measurement_table_and_plots)

                if self.stop_is_requested():
                    final_status = "Stopped"
                    break

        except Exception as e:
            final_status = f"Error: {type(e).__name__}"
            self.show_error_threadsafe("Run Error", f"{type(e).__name__}: {e}")
        finally:
            if relay is None:
                relay = self.get_device_by_type(devices.DenkoviRelayBoard)
            if relay is not None:
                try:
                    with self.relay_lock:
                        relay.all_off()
                    self.append_status("Relay board forced all off.")
                except Exception as e:
                    self.show_error_threadsafe(
                        "Relay All Off Failed", f"{type(e).__name__}: {e}"
                    )
            self.stop_requested = False
            self.stop_event.clear()
            self.run_thread = None
            if final_status == "Done":
                completed_measurements = total_measurements
                progress_detail = "Complete"
            else:
                progress_detail = final_status
            self.set_run_progress(
                completed_measurements,
                total_measurements,
                progress_detail,
            )
            self.set_state(RUN_STATE.DONE, final_status)
            self.ui_after(0, self.update_control_states)
            self.debug(f"Run finished with status: {final_status}.")

    def append_measurement_rows(self, rows: list[dict]):
        with self.data_lock:
            new_data = pd.DataFrame(rows, columns=TEST_DATA_COLUMNS)
            self.test_data = pd.concat([self.test_data, new_data], ignore_index=True)

    def ui_after(self, delay_ms: int, callback):
        if self.closing:
            return None
        try:
            return self.app_root.after(delay_ms, callback)
        except tk.TclError:
            return None

    def stop_is_requested(self) -> bool:
        return self.stop_requested or self.stop_event.is_set()

    def wait_seconds(self, seconds: float) -> bool:
        end_time = time.time() + max(0.0, seconds)
        while time.time() < end_time:
            if self.stop_is_requested():
                return False
            time.sleep(min(0.05, max(0.0, end_time - time.time())))
        return not self.stop_is_requested()

    # request a stop, set all relays off, and update control states
    def request_stop(self):
        if not self.is_sequence_active():
            return
        self.stop_requested = True
        self.stop_event.set()
        self.status_strvar.set("Stopping...")
        self.set_run_progress(detail="Stopping...")
        self.debug("Stop requested.")
        self.safe_relay_all_off_async()
        self.update_control_states()

    # helper to not block UI, on separate thread
    def safe_relay_all_off_async(self):
        threading.Thread(target=self.safe_relay_all_off, daemon=True).start()

    def safe_relay_all_off(self):
        relay = self.get_device_by_type(devices.DenkoviRelayBoard)
        if relay is None:
            return
        try:
            with self.relay_lock:
                relay.all_off()
            self.debug("Relay all-off command sent.")
        except Exception as e:
            print(f"Relay all-off failed: {type(e).__name__}: {e}")

    def new_run(self):
        if self.is_sequence_active():
            return

        if self.has_measurement_data():
            save_choice = messagebox.askyesnocancel(
                "New Run",
                "Measurement data is present. Save it before wiping this run?",
            )
            if save_choice is None:
                self.debug("New run reset canceled.")
                return
            if save_choice and not self.export_results():
                self.debug("New run reset canceled because export did not complete.")
                return

        self.reset_for_new_run()
        self.debug("New run reset.")

    def has_measurement_data(self) -> bool:
        with self.data_lock:
            return not self.test_data.empty

    def reset_for_new_run(self):
        with self.data_lock:
            self.test_data = pd.DataFrame(columns=TEST_DATA_COLUMNS)
        self.freq_step_data = pd.DataFrame(columns=FREQ_STEP_COLUMNS)
        self.custom_freq_data = pd.DataFrame(columns=FREQ_STEP_COLUMNS)

        self.first_freq_strvar.set(DEFAULT_FIRST_FREQ_HZ)
        self.last_freq_strvar.set(DEFAULT_LAST_FREQ_HZ)
        self.points_per_decade_strvar.set(DEFAULT_POINTS_PER_DECADE)
        self.manual_freq_strvar.set("")
        self.probe_settling_strvar.set(str(DEFAULT_PROBE_SETTLING_DELAY))
        self.film_area_mm2_strvar.set(DEFAULT_FILM_AREA_MM2)
        self.film_thickness_um_strvar.set(DEFAULT_FILM_THICKNESS_UM)
        self.selected_plot_freq_strvar.set("")
        self.roll_id_strvar.set(DEFAULT_ROLL_ID)
        self.operator_strvar.set(DEFAULT_OPERATOR)
        self.traceability_confirmed = bool(DEFAULT_ROLL_ID and DEFAULT_OPERATOR)

        for probe_index, probe_vars in enumerate(self.probe_vars, start=1):
            probe_vars["enabled"].set(probe_index == 1)
            probe_vars["label"].set(f"Probe {probe_index:02d}")
            probe_vars["relay"].set(str(probe_index))
        for probe_index, plot_var in enumerate(self.probe_plot_vars, start=1):
            plot_var.set(probe_index == 1)

        self.stop_requested = False
        self.stop_event.clear()
        self.run_thread = None
        self.set_state(RUN_STATE.IDLE, "Idle")
        self.set_run_progress(0, 0, "Progress: idle")
        self.refresh_frequency_table()
        self.refresh_probe_table()
        if hasattr(self, "probe_select_menu"):
            self.build_probe_selector_menu()
        self.relay_status_strvar.set("Relay status not read.")
        self.update_measurement_table_and_plots()

    def update_measurement_table_and_plots(self):
        with self.data_lock:
            display_data = self.test_data.tail(MEASUREMENT_DISPLAY_ROWS).copy()
        self.measurement_table.update_table(display_data)
        self.update_plot_filter_options()
        self.update_plots()

    def set_run_progress(
        self,
        completed: int | None = None,
        total: int | None = None,
        detail: str | None = None,
    ):
        if completed is not None:
            self.run_progress_completed = max(0, int(completed))
        if total is not None:
            self.run_progress_total = max(0, int(total))

        completed_value = self.run_progress_completed
        total_value = self.run_progress_total
        if total_value > 0:
            completed_value = min(completed_value, total_value)
            percent = (completed_value / total_value) * 100.0
            text = (
                f"Progress: {completed_value} / {total_value} "
                f"measurements ({percent:.0f}%)"
            )
            if detail:
                text = f"{text} - {detail}"
        else:
            percent = 0.0
            text = detail or "Progress: idle"

        def apply_progress():
            self.progress_value.set(percent)
            self.progress_strvar.set(text)

        self.ui_after(0, apply_progress)

    def set_state(self, state: RUN_STATE, message: str | None = None):
        self.state = state
        if message is not None:
            self.ui_after(0, lambda: self.status_strvar.set(message))
            self.debug(f"State changed to {state.name}: {message}")
        self.ui_after(0, self.update_control_states)

    def append_status(self, message: str):
        self.debug(message)
        self.ui_after(0, lambda: self.status_strvar.set(message))

    # active state check
    def is_sequence_active(self) -> bool:
        thread_active = self.run_thread is not None and self.run_thread.is_alive()
        return bool(self.state & RUN_STATE.RUNNING) or thread_active

    # control conditions
    def update_control_states(self):
        active = self.is_sequence_active()
        setup_state = "disabled" if active else "normal"
        manual_state = "disabled" if active else "normal"
        idle_state = "disabled" if active else "normal"

        for widget in self.setup_controls:
            self.set_widget_state(widget, setup_state)
        for widget in self.manual_device_controls:
            self.set_widget_state(widget, manual_state)
        for widget in self.manual_probe_controls:
            self.set_widget_state(widget, manual_state)
        for widget in self.idle_controls:
            self.set_widget_state(widget, idle_state)

        self.run_button.configure(state="disabled" if active else "normal")
        self.stop_button.configure(state="normal" if active else "disabled")
        self.export_button.configure(state="disabled" if active else "normal")

    def set_widget_state(self, widget: tk.Widget, state: str):
        try:
            if isinstance(widget, ttk.Combobox):
                widget.configure(state="disabled" if state == "disabled" else "readonly")
            else:
                widget.configure(state=state)
        except tk.TclError:
            pass

    def update_plot_filter_options(self):
        freqs = set()
        try:
            freqs.update(float(freq) for freq in self.get_frequency_list())
        except Exception:
            pass

        with self.data_lock:
            if not self.test_data.empty and "Freq. [Hz]" in self.test_data:
                freqs.update(
                    pd.to_numeric(self.test_data["Freq. [Hz]"], errors="coerce")
                    .dropna()
                    .astype(float)
                    .tolist()
                )

        values = [f"{freq:g}" for freq in sorted(freqs)]
        self.plot_freq_combo.configure(values=values)
        if values and self.selected_plot_freq_strvar.get() not in values:
            preferred = f"{DEFAULT_FOCUS_FREQ_HZ:g}"
            self.selected_plot_freq_strvar.set(
                preferred if preferred in values else values[0]
            )
        elif not values:
            self.selected_plot_freq_strvar.set("")

    def get_selected_plot_frequency(self):
        try:
            return float(self.selected_plot_freq_strvar.get())
        except ValueError:
            return np.nan

    def update_plots(self):
        with self.data_lock:
            data = self.test_data.copy()

        self.update_versus_frequency_plot(data)
        self.update_probe_comparison_plot(data)
        self.update_dielectric_plot(data)
        self.freq_fig.tight_layout(pad=2.0)
        self.probe_fig.tight_layout(pad=2.0)
        self.dielectric_fig.tight_layout(pad=2.0)
        self.freq_canvas.draw_idle()
        self.probe_canvas.draw_idle()
        self.dielectric_canvas.draw_idle()

    def update_versus_frequency_plot(self, dataframe: pd.DataFrame):
        specs = (
            ("Cp [F]", "Cp [F]"),
            ("Df [1]", "Df [1]"),
            (f"ESR [{CHAR_OHM}]", f"ESR [{CHAR_OHM}]"),
        )
        for axis, (column, label) in zip(self.freq_axes, specs):
            axis.clear()
            axis.set_xlabel("Frequency [Hz]")
            axis.set_ylabel(label)
            axis.grid(True, which="both", alpha=0.25)
            axis.set_xscale("log")

            if dataframe.empty:
                axis.set_title("No measurement data")
                continue

            plot_df = dataframe.copy()
            plot_df["Freq. [Hz]"] = pd.to_numeric(
                plot_df["Freq. [Hz]"], errors="coerce"
            )
            if column not in plot_df.columns:
                plot_df[column] = np.nan
            plot_df[column] = pd.to_numeric(plot_df[column], errors="coerce")
            plot_df = plot_df.dropna(subset=["Freq. [Hz]", column])

            for probe_index, group in plot_df.groupby("Probe Index", sort=True):
                group = group.sort_values("Freq. [Hz]")
                label_text = str(group["Probe Label"].iloc[-1])
                if "Probe Color" in group.columns:
                    color = str(group["Probe Color"].iloc[-1])
                else:
                    color = get_default_probe_color(int(probe_index))
                axis.plot(
                    group["Freq. [Hz]"],
                    group[column],
                    marker="o",
                    linewidth=1.3,
                    markersize=3,
                    label=label_text,
                    color=color,
                )
            if not plot_df.empty:
                axis.legend(fontsize="small")

    def update_probe_comparison_plot(self, dataframe: pd.DataFrame):
        selected_probes = set(self.get_selected_plot_probe_indices())
        selected_freq = self.get_selected_plot_frequency()
        specs = (
            ("Cp [F]", "Cp [F]"),
            ("Df [1]", "Df [1]"),
            (f"ESR [{CHAR_OHM}]", f"ESR [{CHAR_OHM}]"),
        )
        for axis, (column, label) in zip(self.probe_axes, specs):
            axis.clear()
            axis.set_ylabel(label)
            axis.grid(True, alpha=0.25)

            if dataframe.empty:
                axis.set_xlabel("Probe Index")
                axis.set_title("No measurement data")
                continue

            plot_df = dataframe.copy()
            plot_df["Freq. [Hz]"] = pd.to_numeric(
                plot_df["Freq. [Hz]"], errors="coerce"
            )
            if column not in plot_df.columns:
                plot_df[column] = np.nan
            plot_df[column] = pd.to_numeric(plot_df[column], errors="coerce")
            plot_df["Probe Index"] = pd.to_numeric(
                plot_df["Probe Index"], errors="coerce"
            )
            plot_df = plot_df[
                plot_df["Probe Index"].isin(selected_probes)
            ].dropna(subset=["Freq. [Hz]", column, "Probe Index"])

            if plot_df.empty:
                axis.set_xlabel("Probe Index")
                axis.set_title("No data for selected probe(s)")
                continue

            measured_freqs = plot_df["Freq. [Hz]"].dropna().astype(float)
            if measured_freqs.empty:
                axis.set_xlabel("Probe Index")
                axis.set_title("No measured frequencies")
                continue

            if np.isfinite(selected_freq):
                nearest_freq = float(
                    measured_freqs.iloc[
                        np.abs(measured_freqs.to_numpy() - selected_freq).argmin()
                    ]
                )
            else:
                nearest_freq = float(measured_freqs.iloc[0])

            freq_slice = plot_df[
                np.isclose(
                    plot_df["Freq. [Hz]"].astype(float),
                    nearest_freq,
                    rtol=1e-9,
                    atol=1e-6,
                )
            ].sort_values("Probe Index")

            if freq_slice.empty:
                axis.set_xlabel(f"Probe Index @ {nearest_freq:g} Hz")
                axis.set_title(f"No data at {nearest_freq:g} Hz")
                continue

            latest_rows = (
                freq_slice.groupby("Probe Index", sort=True)
                .tail(1)
                .sort_values("Probe Index")
            )
            x_values = latest_rows["Probe Index"].astype(int).tolist()
            y_values = latest_rows[column].tolist()
            labels = [
                str(value)
                for value in latest_rows["Probe Label"].fillna("").tolist()
            ]
            colors = [get_default_probe_color(index) for index in x_values]

            axis.plot(
                x_values,
                y_values,
                color="#333333",
                linewidth=1.0,
                marker="o",
                markersize=4,
            )
            axis.scatter(x_values, y_values, c=colors, s=36)
            axis.set_xlabel(f"Probe Index @ {nearest_freq:g} Hz")
            axis.set_xticks(x_values)
            if len(x_values) <= 10 and any(labels):
                axis.set_xticklabels(labels, rotation=30, ha="right")

    def update_dielectric_plot(self, dataframe: pd.DataFrame):
        axis = self.dielectric_axis
        axis.clear()
        axis.set_xlabel("Frequency [Hz]")
        axis.set_ylabel(DIELECTRIC_COLUMN)
        axis.set_xscale("log")
        axis.grid(True, which="both", alpha=0.25)

        if dataframe.empty or DIELECTRIC_COLUMN not in dataframe.columns:
            axis.set_title("No dielectric constant data")
            return

        plot_df = dataframe.copy()
        plot_df["Freq. [Hz]"] = pd.to_numeric(
            plot_df["Freq. [Hz]"], errors="coerce"
        )
        plot_df[DIELECTRIC_COLUMN] = pd.to_numeric(
            plot_df[DIELECTRIC_COLUMN], errors="coerce"
        )
        plot_df = plot_df.dropna(subset=["Freq. [Hz]", DIELECTRIC_COLUMN])

        if plot_df.empty:
            axis.set_title("No dielectric constant data")
            return

        for probe_index, group in plot_df.groupby("Probe Index", sort=True):
            group = group.sort_values("Freq. [Hz]")
            label_text = str(group["Probe Label"].iloc[-1])
            if "Probe Color" in group.columns:
                color = str(group["Probe Color"].iloc[-1])
            else:
                color = get_default_probe_color(int(probe_index))
            axis.plot(
                group["Freq. [Hz]"],
                group[DIELECTRIC_COLUMN],
                marker="o",
                linewidth=1.3,
                markersize=3,
                label=label_text,
                color=color,
            )

        selected_freq = self.get_selected_plot_frequency()
        if np.isfinite(selected_freq):
            axis.axvline(
                selected_freq,
                color="#555555",
                linestyle=":",
                linewidth=0.9,
                alpha=0.65,
            )
        axis.legend(fontsize="small")

    def make_excel_name_safe(self, name: str) -> str:
        invalid_chars = set('[]:*?/\\')
        safe_name = "".join(
            "_" if char in invalid_chars else char for char in str(name).strip()
        )
        if not safe_name:
            safe_name = "Sheet"
        return safe_name[:31]

    def get_probe_sheet_name(self, probe_index: int) -> str:
        return self.make_excel_name_safe(f"Probe_{probe_index:02d}")

    def autosize_excel_columns(self, worksheet, max_width=32):
        for column_cells in worksheet.columns:
            max_length = 0
            column_letter = column_cells[0].column_letter
            for cell in column_cells:
                try:
                    value_length = len(str(cell.value)) if cell.value is not None else 0
                    max_length = max(max_length, value_length)
                except Exception:
                    pass
            worksheet.column_dimensions[column_letter].width = min(
                max_length + 2, max_width
            )

    def get_probe_index_dataframe(self) -> pd.DataFrame:
        rows = []
        for probe_index, probe_vars in enumerate(self.probe_vars, start=1):
            rows.append(
                {
                    "Enabled": bool(probe_vars["enabled"].get()),
                    "Probe Index": probe_index,
                    "Probe Label": probe_vars["label"].get().strip()
                    or f"Probe {probe_index:02d}",
                    "Relay Index": probe_vars["relay"].get().strip(),
                }
            )
        return pd.DataFrame(rows, columns=PROBE_TABLE_COLUMNS)

    def export_results(self):
        if self.is_sequence_active():
            messagebox.showwarning(
                "Run Active",
                "Export is disabled while a run is active.",
            )
            return False
        if not self.ensure_traceability_metadata():
            self.debug("Export canceled before traceability metadata was confirmed.")
            return False

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"RT-BDS_{timestamp}.xlsx"
        filepath = filedialog.asksaveasfilename(
            title="Export RT-BDS Results",
            initialdir=OUTPUT_FILEPATH,
            initialfile=default_name,
            defaultextension=".xlsx",
            filetypes=[("Excel Workbook", "*.xlsx")],
        )
        if not filepath:
            return False

        try:
            self.debug(f"Export started: {filepath}")
            with self.data_lock:
                measurements = self.test_data.copy()
            freq_plan = pd.DataFrame({"Frequency [Hz]": self.get_frequency_list()})
            probe_index = self.get_probe_index_dataframe()
            probe_index_export = probe_index.copy()
            probe_index_export["Probe Sheet"] = probe_index_export[
                "Probe Index"
            ].apply(lambda probe: self.get_probe_sheet_name(int(probe)))
            enabled_probe_count = int(probe_index["Enabled"].sum())
            selected_freq = self.get_selected_plot_frequency()
            roll_id, operator_name = self.get_traceability_values()
            geometry_warning = ""
            try:
                film_area_mm2, film_thickness_um = self.get_film_geometry()
            except ValueError as exc:
                film_area_mm2, film_thickness_um = np.nan, np.nan
                geometry_warning = str(exc)
            if DIELECTRIC_COLUMN not in measurements.columns:
                measurements[DIELECTRIC_COLUMN] = np.nan

            lcr = self.get_device_by_type(devices.KeysightLCR_E4980A)
            relay = self.get_device_by_type(devices.DenkoviRelayBoard)

            metadata = pd.DataFrame(
                [
                    ["Export Time", datetime.now().isoformat(timespec="seconds")],
                    ["Output Path", filepath],
                    ["Roll ID", roll_id],
                    ["Operator", operator_name],
                    [
                        "Relay Board Address",
                        getattr(relay, "address", "Not connected")
                        if relay is not None
                        else "Not connected",
                    ],
                    [
                        "LCR Address",
                        getattr(lcr, "address", "Not connected")
                        if lcr is not None
                        else "Not connected",
                    ],
                    ["Selected Plot Frequency [Hz]", selected_freq],
                    ["Probe Settling Delay [s]", self.get_probe_settling_delay()],
                    ["Film Area [mm^2]", film_area_mm2],
                    ["Film Thickness [um]", film_thickness_um],
                    ["Geometry Parse Warning", geometry_warning],
                    ["Enabled Probe Count", enabled_probe_count],
                    ["Frequency Count", len(freq_plan)],
                    ["Measurement Row Count", len(measurements)],
                ],
                columns=["Field", "Value"],
            )

            summary_rows = [
                ["Measurement Rows", len(measurements)],
                ["Enabled Probes", enabled_probe_count],
                ["Unique Frequencies", 0],
                ["Min Frequency [Hz]", np.nan],
                ["Max Frequency [Hz]", np.nan],
                ["Failed Cp Count", 0],
                ["Failed Dielectric Constant Count", 0],
                ["Failed Df Count", 0],
                ["Failed ESR Count", 0],
            ]

            if not measurements.empty:
                freq_values = pd.to_numeric(
                    measurements["Freq. [Hz]"], errors="coerce"
                )
                cp_values = pd.to_numeric(measurements["Cp [F]"], errors="coerce")
                dielectric_values = pd.to_numeric(
                    measurements[DIELECTRIC_COLUMN], errors="coerce"
                )
                df_values = pd.to_numeric(measurements["Df [1]"], errors="coerce")
                esr_values = pd.to_numeric(
                    measurements[f"ESR [{CHAR_OHM}]"], errors="coerce"
                )
                summary_rows = [
                    ["Measurement Rows", len(measurements)],
                    ["Enabled Probes", enabled_probe_count],
                    ["Unique Frequencies", freq_values.dropna().nunique()],
                    ["Min Frequency [Hz]", freq_values.min()],
                    ["Max Frequency [Hz]", freq_values.max()],
                    ["Failed Cp Count", cp_values.isna().sum()],
                    [
                        "Failed Dielectric Constant Count",
                        dielectric_values.isna().sum(),
                    ],
                    ["Failed Df Count", df_values.isna().sum()],
                    ["Failed ESR Count", esr_values.isna().sum()],
                ]
                for status, count in measurements["Status"].value_counts().items():
                    summary_rows.append([f"Status Count: {status}", count])

                measurements = measurements.sort_values(
                    by=["Probe Index", "Freq. [Hz]", "Timestamp"],
                    ignore_index=True,
                )

            summary = pd.DataFrame(summary_rows, columns=["Metric", "Value"])

            measured_probe_indices: list[int] = []
            if not measurements.empty and "Probe Index" in measurements.columns:
                measured_probe_indices = [
                    int(probe)
                    for probe in pd.to_numeric(
                        measurements["Probe Index"], errors="coerce"
                    )
                    .dropna()
                    .astype(int)
                    .unique()
                ]
            probe_sheet_indices = sorted(
                set(range(1, MAX_PROBES + 1)).union(measured_probe_indices)
            )

            with pd.ExcelWriter(filepath, engine="openpyxl") as writer:
                metadata.to_excel(writer, sheet_name="Metadata", index=False)
                summary.to_excel(writer, sheet_name="Summary", index=False)
                probe_index_export.to_excel(
                    writer, sheet_name="Probe Index", index=False
                )
                freq_plan.to_excel(writer, sheet_name="Frequency Plan", index=False)
                measurements.to_excel(writer, sheet_name="Measurements", index=False)

                for probe_index_value in probe_sheet_indices:
                    sheet_name = self.get_probe_sheet_name(probe_index_value)
                    if measurements.empty:
                        probe_df = pd.DataFrame(columns=TEST_DATA_COLUMNS)
                    else:
                        probe_df = measurements[
                            measurements["Probe Index"] == probe_index_value
                        ].reset_index(drop=True)
                    probe_df.to_excel(writer, sheet_name=sheet_name, index=False)

                workbook = writer.book
                probe_index_sheet = workbook["Probe Index"]
                probe_sheet_column = (
                    probe_index_export.columns.get_loc("Probe Sheet") + 1
                )
                for row_index, sheet_name in enumerate(
                    probe_index_export["Probe Sheet"], start=2
                ):
                    if sheet_name not in workbook.sheetnames:
                        continue
                    cell = probe_index_sheet.cell(
                        row=row_index, column=probe_sheet_column
                    )
                    cell.hyperlink = f"#'{sheet_name}'!A1"
                    try:
                        cell.style = "Hyperlink"
                    except ValueError:
                        pass

                for worksheet in workbook.worksheets:
                    worksheet.freeze_panes = "A2"
                    worksheet.auto_filter.ref = worksheet.dimensions
                    self.autosize_excel_columns(worksheet)

            messagebox.showinfo("Export Complete", f"Results exported to:\n{filepath}")
            self.debug(f"Export complete: {filepath}")
            return True
        except Exception as e:
            messagebox.showerror(
                "Export Failed",
                f"Could not export results:\n{type(e).__name__}: {e}",
            )
            self.debug(f"Export failed: {type(e).__name__}: {e}")
            return False

    def show_error_threadsafe(self, title: str, message: str):
        self.ui_after(0, lambda: messagebox.showerror(title, message))

    def on_close(self):
        self.debug("App close requested.")
        if not messagebox.askyesno("Close RT-BDS?", "Confirm closing."):
            return
        if self.is_sequence_active():
            if not messagebox.askyesno(
                "Close RT-BDS",
                "A run is active. Stop it and close the app?",
            ):
                return
            self.stop_requested = True
            self.stop_event.set()
            self.closing = True
            self.safe_relay_all_off()
            if self.run_thread is not None and self.run_thread.is_alive():
                self.debug("Waiting briefly for run thread to stop.")
                self.run_thread.join(timeout=3.0)
        else:
            self.closing = True

        for device in list(self.device_list):
            try:
                device.close()
            except Exception:
                pass
        try:
            devices.close_resource_manager()
        except Exception:
            pass
        self.debug("App closed.")
        self.app_root.quit()
        self.app_root.destroy()


def main():
    root = tk.Tk()
    RTBDSApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
