"""Entrypoints — installed as the `otto` command (and `python -m otto`):

  otto                       chat in the terminal (default)
  otto dashboard             the browser cockpit → localhost:7777
  otto voice                 talk to it (needs the [voice] extra)
  otto discord               Discord → laptop (needs DISCORD_BOT_TOKEN)
  otto whatsapp              WhatsApp → laptop (needs WHATSAPP_TOKEN, public URL)
  otto brief                 morning briefing (calendar + mail + memory) — as a LOOP
  otto gather                same job as a GRAPH: github, web, calendar and
                             memory fetched together, then one digest
  otto skill install <url>   install a community skill
  otto knowledge add <files> import PDF/DOCX/text into PostgreSQL + pgvector
  otto knowledge list        list imported documents
  otto knowledge search <q>  test hybrid retrieval with citations
"""

from __future__ import annotations

import sys


def main() -> None:
    args = sys.argv[1:]
    if not args:
        from otto.gateway.cli import main as cli_main

        cli_main()
    elif args[0] == "dashboard":
        from otto.ops.dashboard import main as dash_main

        dash_main()
    elif args[0] == "voice":
        from otto.gateway.voice import main as voice_main

        voice_main()
    elif args[0] == "discord":
        from otto.gateway.discord import main as discord_main

        discord_main()
    elif args[0] == "whatsapp":
        from otto.gateway.whatsapp import main as wa_main

        wa_main()
    elif args[0] == "brief":
        from otto.ops.brief import main as brief_main

        brief_main()
    elif args[0] == "gather":
        from otto.ops.gather import main as gather_main

        gather_main()
    elif args[0] == "skill" and len(args) >= 3 and args[1] == "install":
        from otto.memory.procedural.installer import install

        install(args[2])
    elif args[0] == "knowledge":
        from otto.ops.knowledge import main as knowledge_main

        knowledge_main(args[1:])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
