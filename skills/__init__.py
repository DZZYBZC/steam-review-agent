"""
Loader for skill files.
"""

def load_skill(name: str) -> str:
    """
    Load skills

    Parameters:
        name: Filename without the .md extension

    Returns:
        The full text content of the prompt file.
    """
    with open(f"skills/{name}.md") as f:
        return f.read()