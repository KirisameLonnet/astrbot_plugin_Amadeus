import ast

def parse_do_command(cmd_str):
    dummy_code = f"dummy({cmd_str})"
    tree = ast.parse(dummy_code)
    call_node = tree.body[0].value
    kwargs = {}
    for kw in call_node.keywords:
        if isinstance(kw.value, ast.Constant):
            kwargs[kw.arg] = kw.value.value
        elif getattr(ast, "List", None) and isinstance(kw.value, ast.List):
            kwargs[kw.arg] = [elt.value for elt in kw.value.elts if isinstance(elt, ast.Constant)]
    return kwargs

print(parse_do_command('action="Tap", element=[900, 2300]'))
print(parse_do_command('action="Input", text="hello"'))
