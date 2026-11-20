import os
import stat

import yaml
from scripts import synapse_trial_init


def test_refresh_mas_hostnames_preserves_generated_secrets(tmp_path, monkeypatch, capsys):
    output = tmp_path / "secrets" / "matrix"
    output.mkdir(parents=True)
    template = tmp_path / "deploy" / "synapse"
    template.mkdir(parents=True)
    mas_path = output / "mas.yaml"
    mas_path.write_text(
        yaml.safe_dump(
            {
                "http": {
                    "public_base": "https://old.example/",
                    "issuer": "https://old.example/",
                },
                "matrix": {"homeserver": "old.example", "secret": "keep-this-secret"},
                "secrets": {"key": "keep-this-key"},
            }
        )
    )
    template_config = {
        "http": {
            "public_base": "https://auth.commons.theoutpost.network/",
            "issuer": "https://auth.commons.theoutpost.network/",
        },
        "matrix": {"homeserver": "commons.theoutpost.network"},
    }
    (template / "mas.yaml").write_text(yaml.safe_dump(template_config))
    monkeypatch.setattr(synapse_trial_init, "OUT", output)
    monkeypatch.setattr(synapse_trial_init, "ROOT", tmp_path)

    assert synapse_trial_init.refresh_mas_hostnames() == 0

    updated = yaml.safe_load(mas_path.read_text())
    assert updated["http"] == template_config["http"]
    assert updated["matrix"]["homeserver"] == "commons.theoutpost.network"
    assert updated["matrix"]["secret"] == "keep-this-secret"
    assert updated["secrets"] == {"key": "keep-this-key"}
    assert stat.S_IMODE(os.stat(mas_path).st_mode) == 0o600
    assert "keep-this-secret" not in capsys.readouterr().out


def test_refresh_mas_hostnames_requires_existing_generated_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(synapse_trial_init, "OUT", tmp_path / "missing")

    assert synapse_trial_init.refresh_mas_hostnames() == 1
    assert "is missing" in capsys.readouterr().err
