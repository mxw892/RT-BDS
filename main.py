# RT-BDS
# Room temperature broadband spectroscopy
# Spin off version from my HT-BDS project, takes LCR sweeps and logs data at room temperature, but probes are extended to 16.
# Used for characterizing dielectric film

# ===============================================================================
# IMPORTS
# ===============================================================================

from dataclasses import dataclass
import colorsys
import time
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox
from tkinter import filedialog
from typing import Literal
import matplotlib.pyplot as plt
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
import os
import numpy as np
import pandas as pd
from enum import IntFlag, auto
import threading
from datetime import datetime

# ===============================================================================
# DEVICE IMPORTS & OUTPUT SETUP
# ===============================================================================
try:
    import devices as devices
except Exception as e:
    raise ImportError(f"Could not import devices.py: {e}")

# OUTPUT FOLDER SETUP
RUNNING_PATH = os.path.abspath(os.getcwd())
OUTPUT_FOLDER = "RT-BDS Data"
OUTPUT_FILEPATH = os.path.join(RUNNING_PATH, OUTPUT_FOLDER)
os.makedirs(OUTPUT_FILEPATH, exist_ok=True)
DEVICE_NAMES = [dev.name for dev in devices.DEVICE_TYPE_LIST]


# ================================================================================
# CONSTANTS & COLUMNS
# ================================================================================
CHAR_OHM = "\u03a9"
CHAR_THETA = "\u0398"
CHAR_DEG = "\u00b0"
CHAR_DEGC = "\u00b0C"
CHAR_MU = "\u03bc"

UNITS: dict[str, float] = {
    "f": 1e-15,
    "p": 1e-12,
    "n": 1e-9,
    CHAR_MU: 1e-6,
    "u": 1e-6,
    "m": 1e-3,
    "c": 1e-2,
    "": 1,
    "k": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
}


# Columns
FREQ_STEP_COLUMNS = ("Step #", "Frequency [Hz]", "*")
TEST_DATA_COLUMNS = (
    "Probe Index",
    "Probe Label",
    "Probe Color",
    "Freq. [Hz]",
    "Cp [F]",
    "Df [1]",
    f"ESR [{CHAR_OHM}]",
)
PROBE_TABLE_COLUMNS = ("Probe Index", "Probe Label", "Probe Color")

# display constants
DEFAULT_FOCUS_FREQ_HZ = 1000.0
MEASUREMENT_DISPLAY_ROWS = 50
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

# Hardware limits
KEYSIGHT_OVERRANGE_THRESHOLD = 9.8e37
MIN_WAIT_B4_SWITCH = 0.05  #secs


# ================================================================================
# ENUMS & DATACLASSES
# ===============================================================================
class RUN_STATE(IntFlag):
    IDLE = (
        auto()
    )  # App just begun, nothing asked yet. Accept user inputs, parameters, etc.
    PAUSE = (
        auto()
    )  # Paused run. User has requested the run be held in place for intervention or analysis.
    PROBE_SWITCHING = (
        auto()
    )  # Running. Probes are switching for switching frequency measurements.
    LCR_MEASURING = auto()  # Running. LCR is sweeping frequncies for a given probe.
    DONE = auto()  # Run complete / End of programs
    RUNNING = (
        PROBE_SWITCHING | LCR_MEASURING
    )  # Combination of states that would be considered 'running'


# dataclass for storing the run configuration, which can be easily passed around and modified as needed
@dataclass(frozen=True)
class RunConfig:
    device: devices.Device | None
    focus_freq: float
    enable_multiprobe: bool
    probe_configs: tuple[tuple[int, str, str], ...]
    probe_settling_delay: float

# ================================================================================
# HELPER FUNCTIONS
# ===============================================================================


def get_default_probe_color(probe_index):
    if probe_index <= len(PROBE_COLOR_PALETTE):
        return PROBE_COLOR_PALETTE[probe_index - 1]

    hue = ((probe_index - 1) * 0.618033988749895) % 1.0
    red, green, blue = colorsys.hsv_to_rgb(hue, 0.65, 0.78)
    return f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"

def minutes_to_wait(minutes: float) -> str:
    total_seconds = int(round(minutes * 60))
    hours = total_seconds // 3600
    mins = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{mins:02d}:{seconds:02d}"


# highlights all text when entry is selected, and formats the text when deselected
class Entry(tk.Entry):
    textvariable: tk.Variable

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "textvariable" in kwargs.keys():
            self.textvariable = kwargs["textvariable"]
        self.bind("<FocusIn>", self.focus_highlight)
        self.bind("<FocusOut>", self.format_input)

    def focus_highlight(self, *args):
        self.selection_range(0, "end")

    def format_input(self, *args):
        self.textvariable.set(self.textvariable.get())


# alternating color for table rows, with function to update the whole table based on a given dataframe
class Table(ttk.Treeview):
    def __init__(self, header_widths, increment=True, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tag_configure("evenrow", background="#E8E8E8")
        self.tag_configure("oddrow", background="#FFFFFF")
        self.index_inc = increment
        columns = self.cget("columns")
        self.set_headings(columns, header_widths)

    # sets the column widths and headings based on the given lists
    def set_headings(self, column_list, width_list):
        self.column("#0", width=0, stretch=True)
        self.heading("#0", text="", anchor="w")
        for i, col in enumerate(column_list):
            width = width_list[i] if i < len(width_list) else 80
            self.column(col, minwidth=50, width=width, stretch=True, anchor="w")
            self.heading(col, text=col, anchor="w")

    # updates the whole table based on the given dataframe, with alternating row colors
    def update_table(self, dataframe: pd.DataFrame):
        # Add data with alternating row colors
        self.delete(*self.get_children())
        for i, data in enumerate(dataframe.itertuples(index=False, name=None)):
            if self.index_inc:
                inc_data = [data[0] + 1]
                inc_data.extend(data[1:])
                data = inc_data
            if i % 2 == 0:
                self.insert(parent="", index="end", values=data, tags="evenrow")
            else:
                self.insert(parent="", index="end", values=data, tags="oddrow")

    # adds a single row to the end of the table, with alternating row color
    def appendRow(self, data):
        i = len(self.get_children())
        if self.index_inc:
            data = [i + 1] + list(data)
        tag = "evenrow" if i % 2 == 0 else "oddrow"
        self.insert(parent="", index="end", values=data, tags=tag)
        print("Appending row to table:", data)

# Plotting
