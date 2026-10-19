"""`prayog help [command]`: the same text as --help, limited to what this person can use."""
from prayog.context import Context


def setup_help(p):
    p.add_argument("topic", nargs="?", metavar="command", help="a command to describe")
    p.set_defaults(func=show_help)


def show_help(ctx: Context, args) -> None:
    parser = ctx.build_parser(ctx.creds)
    if args.topic is None:
        parser.print_help()
        return
    if args.topic not in {c.name for c in ctx.available(ctx.creds)}:
        raise ctx.denial(args.topic, ctx.creds)      # same answer as running the command itself
    try:
        parser.parse_args([args.topic, "--help"])
    except SystemExit:                               # argparse prints the help, then exits 0
        pass
