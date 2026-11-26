"""Add the account-model hooks to the pinned Open WebUI wheel, before service restart."""
import importlib.metadata
import importlib.util
import os
from pathlib import Path
import shutil
import tempfile


def edit(path, before, after):
    original = path.read_text()
    if after in original:
        return
    if original.count(before) != 1:
        raise RuntimeError(f"Unexpected Open WebUI source in {path.name}")
    content = original.replace(before, after)
    if path.suffix == ".py":
        compile(content, str(path), "exec")
    backup = path.with_suffix(path.suffix + ".office-original")
    if not backup.exists():
        shutil.copy2(path, backup)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        stream.write(content.encode())
        temp = Path(stream.name)
    temp.chmod(path.stat().st_mode)
    os.replace(temp, path)


def install():
    if importlib.metadata.version("open-webui") != "0.13.5":
        raise RuntimeError("Verify personal-model integration before upgrading Open WebUI")
    root = Path(importlib.util.find_spec("open_webui").origin).parent
    tools = root / "utils" / "tools.py"
    line = "    # Time utilities - available for date calculations"
    edit(tools, line, """    if (is_builtin_tool_enabled('files') and get_model_capability('file_upload')
            and await has_user_chat_permission('file_upload')):
        from workbook_export import create_excel_file
        builtin_functions.append(create_excel_file)

""" + line)
    base = root / "main.py"
    api = root / "routers" / "openai.py"
    line = "    models.extend(model for model in (await get_tether_models(user)).values() if model['id'] not in known)"
    edit(base, line, line + "\n    from personal import catalog\n    models.extend(await catalog(user))")
    line = "    return (await get_tether_models(user)).get(model_id)"
    edit(api, line, """    if model_id.startswith('personal:'):
        from personal import personal_model
        return await personal_model(user, model_id)
""" + line)
    line = """async def generate_chat_completion(
    request: Request,
    form_data: dict,
    user=Depends(get_verified_user),
):"""
    edit(api, line, line + """
    if str(form_data.get('model', '')).startswith('personal:'):
        from personal import complete
        return await complete(user, form_data)
""")


if __name__ == "__main__":
    install()
