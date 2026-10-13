"""Scripted CLI answers usable from unit and integration test modules."""

from acp_gateway.cli.wizard import _choice


class Answers:
    def __init__(self, **answers):
        self.answers = answers

    def ask(self, label, default="", validate=None, **kwargs):
        value = self.answers.get(label, default)
        return validate(value) if validate else value

    def yes(self, label, default=False):
        return self.answers.get(label, False)

    def choice(self, label, choices, default):
        return _choice(self.answers.get(label, default), choices)


def save_answers(**kwargs):
    return Answers(**{"Save these settings?": True, **kwargs})
