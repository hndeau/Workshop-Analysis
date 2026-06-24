"""Interactive prompt helpers used by the command interpreter."""

from .common import write_section


def prompt_choice(title, choices, label_property="Name", default_id=None, id_property="Id"):
    write_section(title)
    for index, choice in enumerate(choices, start=1):
        label = str(choice.get(label_property, ""))
        choice_id = str(choice.get(id_property, ""))
        suffix = " [default]" if default_id and choice_id == default_id else ""
        print("[{0}] {1}{2}".format(index, label, suffix))
        description = choice.get("Description")
        if description:
            print("    {0}".format(description))

    while True:
        answer = input("Select a number: ").strip()
        if not answer and default_id:
            for choice in choices:
                if str(choice.get(id_property, "")) == default_id:
                    return choice
        try:
            index = int(answer)
        except ValueError:
            index = 0
        if 1 <= index <= len(choices):
            return choices[index - 1]
        print("WARNING: Invalid selection.")

def prompt_yes_no(question, default=True):
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        answer = input("{0} {1}: ".format(question, suffix)).strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("WARNING: Please answer yes or no.")

def prompt_non_empty(prompt, default=None):
    while True:
        suffix = " [{0}]".format(default) if default else ""
        value = input("{0}{1}: ".format(prompt, suffix))
        if not value.strip() and default:
            return str(default)
        if value.strip():
            return value.strip()
        print("WARNING: A value is required.")
