"""CLI entry point. Real subcommands land during implementation; see openspec/."""

import click


@click.group(help="tai-mcp-ssh: MCP server for remote Linux admin via SSH.")
@click.version_option()
def main() -> None:
    pass


@main.command()
def serve() -> None:
    """Start the MCP server over stdio."""
    raise click.ClickException("Not implemented yet; see openspec/ for the design.")


if __name__ == "__main__":
    main()
