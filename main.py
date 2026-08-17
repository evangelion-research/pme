import click


@click.command()
@click.option('--name',default=1,help="Name of the package")
def init():
    click.echo("welcome to pme")

def main():
    init()

if __name__ == "__main__":
    main()
