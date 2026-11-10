"""PyInstaller entry point. Running src/bad_ass_mcp/server.py directly makes
it a top-level script, where its relative imports fail ("attempted relative
import with no known parent package"); importing the package keeps them."""

from bad_ass_mcp.server import main

if __name__ == "__main__":
    main()
