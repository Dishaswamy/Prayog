"""`prayog project ...`: your ML projects on the Prayog server."""
from prayog.context import Context
from prayog.errors import CliError, USAGE

MAX_NAME = 120
MAX_DESCRIPTION = 4000


def setup_project(p):
    sub = p.add_subparsers(dest="project_cmd", metavar="<subcommand>", required=True)

    c = sub.add_parser("create", help="create a project on the server",
                       description="Create a project. Its name must be unique among your projects "
                                   "(upper/lower case is ignored). The server gives it a unique project ID.")
    c.add_argument("name", help="project name, up to 120 characters")
    c.add_argument("-d", "--description", help="what the project is for")
    c.set_defaults(func=create)


def create(ctx: Context, args) -> None:
    name = args.name.strip()
    if not name:
        raise CliError("Project name cannot be blank.", USAGE)
    if len(name) > MAX_NAME:
        raise CliError(f"Project name is too long ({len(name)} characters; the limit is {MAX_NAME}).", USAGE)
    if args.description is not None and len(args.description) > MAX_DESCRIPTION:
        raise CliError(f"Description is too long (the limit is {MAX_DESCRIPTION} characters).", USAGE)

    project = ctx.client().post("/mlflow/projects", json={"name": name, "description": args.description})
    print(f"Created project '{project['name']}'.")
    print(f"Project ID: {project['project_id']}")
