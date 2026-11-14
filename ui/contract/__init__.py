"""The theme contract: the only ui code a theme imports besides ui.kit.

Keep this module free of imports: gui.py and ui.entry import ui.contract.manifest (and with it this
package) before NiceGUI, in the server process and in pywebview's window process.
"""

CONTRACT_VERSION: tuple[int, int] = (1, 2)    # 1.2 adds Speak call controls
