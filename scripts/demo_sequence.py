# Exact typing sequence for the bad-ass-mcp showcase recording.
# Each tuple is (action, arg):
#   ("type", text)  →  type_text(handle_id, text)
#   ("key", key)    →  press_key(key)
#
# Strategy: type each line, then Return + Home to land at col 0
# (avoids CodeMirror auto-indent stacking on subsequent lines).
# Empty lines are a bare Return + Home.

SEQUENCE = [
    # Title
    ("type", "# Maxwell's Equations"),
    ("key", "Return"),
    ("key", "Home"),
    ("key", "Return"),
    ("key", "Home"),
    # Intro line
    ("type", "The four fundamental laws of electromagnetism:"),
    ("key", "Return"),
    ("key", "Home"),
    ("key", "Return"),
    ("key", "Home"),
    # Open aligned KaTeX block
    ("type", r"$$\begin{aligned}"),
    ("key", "Return"),
    ("key", "Home"),
    # Four Maxwell equations
    ("type", r"\nabla \cdot \mathbf{E} &= \frac{\rho}{\varepsilon_0} \\"),
    ("key", "Return"),
    ("key", "Home"),
    ("type", r"\nabla \cdot \mathbf{B} &= 0 \\"),
    ("key", "Return"),
    ("key", "Home"),
    ("type", r"\nabla \times \mathbf{E} &= -\frac{\partial \mathbf{B}}{\partial t} \\"),
    ("key", "Return"),
    ("key", "Home"),
    (
        "type",
        r"\nabla \times \mathbf{B} &= \mu_0\mathbf{J} +"
        r" \mu_0\varepsilon_0\frac{\partial \mathbf{E}}{\partial t}",
    ),
    ("key", "Return"),
    ("key", "Home"),
    # Close aligned block
    (r"type", r"\end{aligned}$$"),
    ("key", "Return"),
    ("key", "Home"),
    ("key", "Return"),
    ("key", "Home"),
    # Inline formula line
    ("type", r"The speed of light emerges directly: $c = \dfrac{1}{\sqrt{\mu_0\varepsilon_0}}$"),
    ("key", "Return"),
    ("key", "Home"),
    ("key", "Return"),
    ("key", "Home"),
    # Mermaid section heading
    ("type", "## Field Coupling"),
    ("key", "Return"),
    ("key", "Home"),
    ("key", "Return"),
    ("key", "Home"),
    # Open mermaid fence
    ("type", "```mermaid"),
    ("key", "Return"),
    ("key", "Home"),
    # Diagram body
    ("type", "sequenceDiagram"),
    ("key", "Return"),
    ("key", "Home"),
    ("type", "participant E as E field"),
    ("key", "Return"),
    ("key", "Home"),
    ("type", "participant B as B field"),
    ("key", "Return"),
    ("key", "Home"),
    ("type", "participant P as Photon"),
    ("key", "Return"),
    ("key", "Home"),
    ("type", "E->>B: curl E = -dB/dt"),
    ("key", "Return"),
    ("key", "Home"),
    ("type", "B->>E: curl B = mu*dE/dt"),
    ("key", "Return"),
    ("key", "Home"),
    ("type", "E->>P: oscillate"),
    ("key", "Return"),
    ("key", "Home"),
    ("type", "B->>P: oscillate"),
    ("key", "Return"),
    ("key", "Home"),
    ("type", "P-->>E: propagate at c"),
    ("key", "Return"),
    ("key", "Home"),
    # Close mermaid fence
    ("type", "```"),
    ("key", "Return"),
    ("key", "Home"),
]
