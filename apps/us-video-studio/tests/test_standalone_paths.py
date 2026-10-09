"""Independent application code, configured business inputs, disposable data."""
import pytest

from studio import paths
from studio.learning import validate_method_packages
from studio.connections import ConnectionStore


def test_business_root_requires_explicit_absolute_existing_library(monkeypatch, tmp_path):
    monkeypatch.delenv('STUDIO_BUSINESS_ROOT', raising=False)
    with pytest.raises(ValueError, match='STUDIO_BUSINESS_ROOT'):
        paths.business_root()
    monkeypatch.setenv('STUDIO_BUSINESS_ROOT', 'relative/business')
    with pytest.raises(ValueError, match='absolute'):
        paths.business_root()
    monkeypatch.setenv('STUDIO_BUSINESS_ROOT', str(tmp_path))
    with pytest.raises(ValueError, match='learning library'):
        paths.business_root()
    assert list(tmp_path.iterdir()) == []


def test_application_cards_ignore_old_application_in_business_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, 'BUSINESS_ROOT', tmp_path)
    relative = 'apps/us-video-studio/docs/methods/visual-design.md'
    old_card = tmp_path / relative
    old_card.parent.mkdir(parents=True)
    old_card.write_text('Obsolete application method', encoding='utf-8')
    actual = paths.document_path(tmp_path, relative)
    assert actual == paths.PROJECT_ROOT / relative
    assert 'Obsolete application method' not in actual.read_text(encoding='utf-8-sig')
    assert paths.document_path(tmp_path, 'tracks/us/example.md') == tmp_path / 'tracks/us/example.md'


def test_document_path_rejects_escape(tmp_path):
    with pytest.raises(ValueError, match='escapes'):
        paths.document_path(tmp_path, '../outside.md')


def test_application_and_business_sources_cannot_hold_credentials(monkeypatch, tmp_path):
    business = tmp_path / 'business'
    monkeypatch.setattr(paths, 'BUSINESS_ROOT', business)
    for root in (paths.PROJECT_ROOT / 'test-data', business / 'test-data'):
        with pytest.raises(ValueError):
            ConnectionStore(root)
        assert not root.exists()


def test_server_rejects_source_data_before_creating_any_files(monkeypatch, tmp_path):
    from studio.server import build_app
    from studio.config import Settings
    business = tmp_path / 'business'
    monkeypatch.setattr(paths, 'BUSINESS_ROOT', business)
    with pytest.raises(ValueError, match='outside'):
        build_app(Settings(data_root=business / 'data', base_url='https://example.invalid'))
    assert not business.exists()


def test_comparison_cli_rejects_business_output_before_writing(monkeypatch, tmp_path):
    import importlib.util
    business = tmp_path / 'business'
    monkeypatch.setenv('STUDIO_BUSINESS_ROOT', str(business))
    spec = importlib.util.spec_from_file_location('comparison_test', paths.PROJECT_ROOT / 'scripts/creative_comparison.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(ValueError):
        module.write_new(business / 'comparison.md', 'Synthetic report')
    assert not business.exists()


def test_offline_business_fixture_and_current_application_cards_validate():
    assert validate_method_packages() == []
