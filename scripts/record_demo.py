#!/usr/bin/env python3
"""Record the Maxwell's Equations demo GIF for bad-ass-mcp."""

import sys
import time

sys.path.insert(0, "/usr/lib/python3/dist-packages")
sys.path.insert(0, "/home/kali/code/bad-ass-mcp/src")

from bad_ass_mcp.backend.linux import LinuxBackend

b = LinuxBackend()

wins = b.list_windows()
mtc = next(w for w in wins if "markthecrab" in w.name.lower())
print(f"Found: {mtc.name} (pid {mtc.id})")

# Close any open menus
els = b.find_elements(mtc.id, role="entry")
if els:
    b.press_key("Escape")
    time.sleep(0.2)
    b.press_key("Escape")
    time.sleep(0.2)

# Fresh document
new_btn = b.find_elements(mtc.id, role="button", name="New (Ctrl+N)")
if new_btn:
    b.click(new_btn[0].id)
    time.sleep(0.8)

els = b.find_elements(mtc.id, role="entry")
if not els:
    print("ERROR: editor entry not found")
    sys.exit(1)
entry_id = els[0].id
print(f"Editor entry: {entry_id}")

print()
print("Click inside the MarkTheCrab editor now — recording starts in 3 seconds...")
for i in (3, 2, 1):
    print(f"  {i}...")
    time.sleep(1)
print("Go!")

# Start recording cropped to MarkTheCrab
rec = b.start_recording(mtc.id, fps=15)
time.sleep(0.4)

preamble = (
    "# Maxwell's Equations\n"
    "\n"
    "The four fundamental laws of electromagnetism:\n"
    "\n"
    "$$\\begin{aligned}\n"
    "\\nabla \\cdot \\mathbf{E} &= \\frac{\\rho}{\\varepsilon_0} \\\\\n"
    "\\nabla \\cdot \\mathbf{B} &= 0 \\\\\n"
    "\\nabla \\times \\mathbf{E} &= -\\frac{\\partial \\mathbf{B}}{\\partial t} \\\\\n"
    "\\nabla \\times \\mathbf{B} &= \\mu_0\\mathbf{J} +"
    " \\mu_0\\varepsilon_0\\frac{\\partial \\mathbf{E}}{\\partial t}\n"
    "\\end{aligned}$$\n"
    "\n"
    "The speed of light emerges directly: $c = \\dfrac{1}{\\sqrt{\\mu_0\\varepsilon_0}}$\n"
    "\n"
    "## Field Coupling\n"
    "\n"
)

diagram = (
    "\nsequenceDiagram\n"
    "participant E as E field\n"
    "participant B as B field\n"
    "participant P as Photon\n"
    "E->>B: curl E = -dB/dt\n"
    "B->>E: curl B = mu*dE/dt\n"
    "E->>P: oscillate\n"
    "B->>P: oscillate\n"
    "P-->>E: propagate at c\n"
    "```"
)

steps = [
    {"action": "type", "handle": entry_id, "text": preamble},
    {"action": "sleep", "seconds": 0.5},
    # Mermaid fence — triggers CodeMirror code-block DOM mutations (D-Bus flood).
    # The 3.5s sleep lets the AT-SPI event queue drain before the next injection.
    {"action": "type", "handle": entry_id, "text": "```mermaid"},
    {"action": "sleep", "seconds": 3.5},
    {"action": "type", "handle": entry_id, "text": diagram},
    {"action": "sleep", "seconds": 4.0},
]

print("Running sequence...")
results = b.run_sequence(steps)
for r in results:
    print(r)

out = "/home/kali/code/bad-ass-mcp/docs/demo.gif"
print(f"Saving GIF to {out} ...")
b.stop_recording(rec, out)
print("Done.")
