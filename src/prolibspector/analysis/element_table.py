"""Shared periodic-table grid for element selection dialogs.

Used by the spectrum Search Element window and the mapping element
selector so tile drawing, group colors, spacer layout, and the
selection-cap behavior live in one place.
"""

from __future__ import annotations

import tkinter as tk

from prolibspector.core.ui_scale import scaled_font, scaled_int

MAX_SELECTED_ELEMENTS = 20
SELECTED_TILE_COLOR = "#C2B2A6"

# Colors for each group
GROUP_COLORS = {
    "alkali_metals": "#244d57",
    "alkaline_earth_metals": "#622e39",
    "transition_metals": "#433c65",
    "post_transition_metals": "#2f4d47",
    "metalloids": "#523e1b",
    "nonmetals": "#2a4165",
    "halogens": "#287F6B",
    "noble_gases": "#623846",
    "lanthanides": "#004a77",
    "actinides": "#613b28",
    "unknown": "#46474c",
}

# Element format: (symbol, name, atomic_number, column, row, group)
ELEMENTS = [

#Nonmetals:
("H", "Hydrogen", 1, 0, 0, "nonmetals"), ("C", "Carbon", 6, 13, 1, "nonmetals"), ("N", "Nitrogen", 7, 14, 1, "nonmetals"), ("O", "Oxygen", 8, 15, 1, "nonmetals"), ("P", "Phosphorus", 15, 14, 2, "nonmetals"), ("S", "Sulfur", 16, 15, 2, "nonmetals"), ("Se", "Selenium", 34, 15, 3, "nonmetals"),
#Alkali metals:
("Li", "Lithium", 3, 0, 1, "alkali_metals"), ("Na", "Sodium", 11, 0, 2, "alkali_metals"), ("K", "Potassium", 19, 0, 3, "alkali_metals"), ("Rb", "Rubidium", 37, 0, 4, "alkali_metals"), ("Cs", "Cesium", 55, 0, 5, "alkali_metals"), ("Fr", "Francium", 87, 0, 6, "alkali_metals"),
#Alkaline earth metals:
("Be", "Beryllium", 4, 1, 1, "alkaline_earth_metals"), ("Mg", "Magnesium", 12, 1, 2, "alkaline_earth_metals"), ("Ca", "Calcium", 20, 1, 3, "alkaline_earth_metals"), ("Sr", "Strontium", 38, 1, 4, "alkaline_earth_metals"), ("Ba", "Barium", 56, 1, 5, "alkaline_earth_metals"), ("Ra", "Radium", 88, 1, 6, "alkaline_earth_metals"),
#Halogens:
("F", "Fluorine", 9, 16, 1, "halogens"), ("Cl", "Chlorine", 17, 16, 2, "halogens"), ("Br", "Bromine", 35, 16, 3, "halogens"), ("I", "Iodine", 53, 16, 4, "halogens"), ("At", "Astatine", 85, 16, 5, "halogens"),
#Noble gases:
("He", "Helium", 2, 17, 0, "noble_gases"), ("Ne", "Neon", 10, 17, 1, "noble_gases"), ("Ar", "Argon", 18, 17, 2, "noble_gases"), ("Kr", "Krypton", 36, 17, 3, "noble_gases"), ("Xe", "Xenon", 54, 17, 4, "noble_gases"), ("Rn", "Radon", 86, 17, 5, "noble_gases"), ("Og", "Oganesson", 118, 17, 6, "unknown"),
#Metals:
("Al", "Aluminium", 13, 12, 2, "post_transition_metals"), ("Ga", "Gallium", 31, 12, 3, "post_transition_metals"), ("In", "Indium", 49, 12, 4, "post_transition_metals"), ("Tl", "Thallium", 81, 12, 5, "post_transition_metals"), ("Sn", "Tin", 50, 13, 4, "post_transition_metals"), ("Pb", "Lead", 82, 13, 5, "post_transition_metals"), ("Bi", "Bismuth", 83, 14, 5, "post_transition_metals"),
#Metalloids:
("B", "Boron", 5, 12, 1, "metalloids"), ("Si", "Silicon", 14, 13, 2, "metalloids"), ("Ge", "Germanium", 32, 13, 3, "metalloids"), ("As", "Arsenic", 33, 14, 3, "metalloids"), ("Sb", "Antimony", 51, 14, 4, "metalloids"), ("Te", "Tellurium", 52, 15, 4, "metalloids"), ("Po", "Polonium", 84, 15, 5, "metalloids"),
#Transition metals:
("Sc", "Scandium", 21, 2, 3, "transition_metals"), ("Ti", "Titanium", 22, 3, 3, "transition_metals"), ("V", "Vanadium", 23, 4, 3, "transition_metals"), ("Cr", "Chromium", 24, 5, 3, "transition_metals"), ("Mn", "Manganese", 25, 6, 3, "transition_metals"), ("Fe", "Iron", 26, 7, 3, "transition_metals"), ("Co", "Cobalt", 27, 8, 3, "transition_metals"), ("Ni", "Nickel", 28, 9, 3, "transition_metals"), ("Cu", "Copper", 29, 10, 3, "transition_metals"), ("Zn", "Zinc", 30, 11, 3, "transition_metals"), ("Y", "Yttrium", 39, 2, 4, "transition_metals"), ("Zr", "Zirconium", 40, 3, 4, "transition_metals"), ("Nb", "Niobium", 41, 4, 4, "transition_metals"), ("Mo", "Molybdenum", 42, 5, 4, "transition_metals"), ("Tc", "Technetium", 43, 6, 4, "transition_metals"), ("Ru", "Ruthenium", 44, 7, 4, "transition_metals"), ("Rh", "Rhodium", 45, 8, 4, "transition_metals"), ("Pd", "Palladium", 46, 9, 4, "transition_metals"), ("Ag", "Silver", 47, 10, 4, "transition_metals"), ("Cd", "Cadmium", 48, 11, 4, "transition_metals"), ("Hf", "Hafnium", 72, 3, 5, "transition_metals"), ("Ta", "Tantalum", 73, 4, 5, "transition_metals"), ("W", "Tungsten", 74, 5, 5, "transition_metals"), ("Re", "Rhenium", 75, 6, 5, "transition_metals"), ("Os", "Osmium", 76, 7, 5, "transition_metals"), ("Ir", "Iridium", 77, 8, 5, "transition_metals"), ("Pt", "Platinum", 78, 9, 5, "transition_metals"), ("Au", "Gold", 79, 10, 5, "transition_metals"), ("Hg", "Mercury", 80, 11, 5, "transition_metals"), ("Rf", "Rutherfordium", 104, 3, 6, "transition_metals"), ("Db", "Dubnium", 105, 4, 6, "transition_metals"), ("Sg", "Seaborgium", 106, 5, 6, "transition_metals"), ("Bh", "Bohrium", 107, 6, 6, "transition_metals"), ("Hs", "Hassium", 108, 7, 6, "transition_metals"),
#Lanthanides:
("La", "Lanthanum", 57, 2, 5, "lanthanides"), ("Ce", "Cerium", 58, 3, 8, "lanthanides"), ("Pr", "Praseodymium", 59, 4, 8, "lanthanides"), ("Nd", "Neodymium", 60, 5, 8, "lanthanides"), ("Pm", "Promethium", 61, 6, 8, "lanthanides"), ("Sm", "Samarium", 62, 7, 8, "lanthanides"), ("Eu", "Europium", 63, 8, 8, "lanthanides"), ("Gd", "Gadolinium", 64, 9, 8, "lanthanides"), ("Tb", "Terbium", 65, 10, 8, "lanthanides"), ("Dy", "Dysprosium", 66, 11, 8, "lanthanides"), ("Ho", "Holmium", 67, 12, 8, "lanthanides"), ("Er", "Erbium", 68, 13, 8, "lanthanides"), ("Tm", "Thulium", 69, 14, 8, "lanthanides"), ("Yb", "Ytterbium", 70, 15, 8, "lanthanides"), ("Lu", "Lutetium", 71, 16, 8, "lanthanides"),
#Actinides:
("Ac", "Actinium", 89, 2, 6, "actinides"), ("Th", "Thorium", 90, 3, 9, "actinides"), ("Pa", "Protactinium", 91, 4, 9, "actinides"), ("U", "Uranium", 92, 5, 9, "actinides"), ("Np", "Neptunium", 93, 6, 9, "actinides"), ("Pu", "Plutonium", 94, 7, 9,"actinides"), ("Am", "Americium", 95, 8, 9, "actinides"), ("Cm", "Curium", 96, 9, 9, "actinides"), ("Bk", "Berkelium", 97, 10, 9, "actinides"), ("Cf", "Californium", 98, 11, 9, "actinides"), ("Es", "Einsteinium", 99, 12, 9, "actinides"), ("Fm", "Fermium", 100, 13, 9, "actinides"), ("Md", "Mendelevium", 101, 14, 9, "actinides"), ("No", "Nobelium", 102, 15, 9, "actinides"), ("Lr", "Lawrencium", 103, 16, 9, "actinides"),
#Unknown:
("Mt", "Meitnerium", 109, 8, 6, "unknown"), ("Ds", "Darmstadtium", 110, 9, 6, "unknown"), ("Rg", "Roentgenium", 111, 10, 6, "unknown"), ("Cn", "Copernicium", 112, 11, 6, "unknown"), ("Nh", "Nihonium", 113, 12, 6, "unknown"), ("Fl", "Flerovium", 114, 13, 6, "unknown"), ("Mc", "Moscovium", 115, 14, 6, "unknown"), ("Lv", "Livermorium", 116, 15, 6, "unknown"), ("Ts", "Tennessine", 117, 16, 6, "unknown"), ("Og", "Oganesson", 118, 17, 6, "unknown")]


class ElementTile(tk.Canvas):
    """Canvas-drawn periodic-table tile toggling membership in a shared list."""

    def __init__(self, master, element, name, number, *, selected, scale, bg_color, width, height, on_refused=None, on_toggle=None):
        super().__init__(master, width=width, height=height, bg=bg_color, highlightthickness=0)
        self.element = element
        self.bg_color = bg_color
        self._selected = selected
        self._on_refused = on_refused
        self._on_toggle = on_toggle
        self.create_text(width / 2, height / 2 - scaled_int(2, scale), text=element, fill="white", font=scaled_font(14, family="lato", scale=scale))
        self.create_text(width / 2, height / 2 + scaled_int(18, scale), text=name, fill="white", font=scaled_font(6, family="lato", scale=scale))
        self.create_text(scaled_int(10, scale), scaled_int(5, scale), text=str(number), fill="white", font=scaled_font(8, family="lato", scale=scale))
        if element in selected:
            self.configure(bg=SELECTED_TILE_COLOR)
        self.bind("<Button-1>", self._on_click)

    def _on_click(self, _event):
        if self.element in self._selected:
            self._selected.remove(self.element)
            self.configure(bg=self.bg_color)
        elif len(self._selected) >= MAX_SELECTED_ELEMENTS:
            # Cap refused: leave the tile unselected so the visual state
            # always matches the selection list.
            if self._on_refused is not None:
                self._on_refused(self.element)
            return
        else:
            self._selected.append(self.element)
            self.configure(bg=SELECTED_TILE_COLOR)
        if self._on_toggle is not None:
            self._on_toggle(self.element)


def build_periodic_grid(table_frame, *, selected, scale, on_refused=None, on_toggle=None):
    """Populate *table_frame* with the full periodic table.

    *selected* is a mutable list of element symbols shared with the caller;
    tiles toggle membership directly (respecting MAX_SELECTED_ELEMENTS) and
    render pre-selected symbols highlighted."""
    # Spacer rows/columns between the main block, the first columns, and the
    # lanthanide/actinide rows.
    table_frame.grid_rowconfigure(0, minsize=20)
    table_frame.grid_columnconfigure(1, minsize=20)
    table_frame.grid_columnconfigure(2, minsize=20)
    table_frame.grid_columnconfigure(3, minsize=20)
    table_frame.grid_rowconfigure(7, minsize=20)
    table_frame.grid_rowconfigure(9, minsize=20)
    table_frame.grid_columnconfigure(10, minsize=20)

    tile_size = scaled_int(60, scale)
    for element, name, number, column, row, group in ELEMENTS:
        tile = ElementTile(
            table_frame,
            element,
            name,
            number,
            selected=selected,
            scale=scale,
            bg_color=GROUP_COLORS[group],
            width=tile_size,
            height=tile_size,
            on_refused=on_refused,
            on_toggle=on_toggle,
        )
        tile.grid(row=row, column=column, padx=1, pady=1)
