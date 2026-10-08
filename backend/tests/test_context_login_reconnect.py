"""Management-page startup must never suppress a desktop source-login window."""
import io
from contextlib import contextmanager
from types import SimpleNamespace
import pytest
from test_context_scheduling import env
from test_context_connection_runner import terminal
from test_context_source_pages import account_payload
from collection_context import launcher
from collection_context.application.contracts import ContextError
from collection_context.application.connection_runner import ConnectionRunner
from collection_context.library.store import LibraryStore
from collection_context.sources.account import self_account
from collection_context.workflows.connection import ConnectionCatalog


@pytest.mark.parametrize('desktop,no_browser,explicit,expected',[
    (True,True,None,False),(True,False,None,False),
    (False,True,None,True),(False,False,None,True),
    (True,True,True,True),(True,True,False,False),
])
def test_management_autolaunch_and_source_visibility_are_independent(tmp_path,monkeypatch,desktop,no_browser,explicit,expected):
    root=tmp_path/'isolated';store=LibraryStore.initialize(root);store.close()
    observed=[]
    @contextmanager
    def capture(capabilities,*,headless):
        observed.append(headless)
        raise ContextError('fixture_stop','Stop before HTTP, browser, or worker startup')
        yield  # Context-manager shape; never starts external resources.
    monkeypatch.setattr(launcher,'_desktop_available',lambda:desktop)
    monkeypatch.setattr(launcher,'startup_report',lambda *_:{'capabilities':{'ready_for_management_page':True},'listen':{'available':True}})
    monkeypatch.setattr(launcher,'launcher_resources',capture)
    with pytest.raises(ContextError,match='Stop before HTTP'):
        launcher.launch(root,port=8787,initialize_empty=False,no_browser=no_browser,
                        source_connect_headless=explicit,output=io.StringIO())
    assert observed==[expected]


def test_logout_then_login_opens_visible_browser_and_keeps_materials(env,tmp_path,monkeypatch):
    store=env[0];catalog=ConnectionCatalog(store)
    catalog.record(self_account(account_payload()),expected_version=None)
    store.upsert({'native_id':'77','title':'保留资料'},kind='saved',scope_id='s_test')
    before=store.snapshot()['items'];events=[]
    class Browser:
        def __init__(self,path,*,headless):self.headless=headless;events.append(('browser',headless))
        def __enter__(self):return self
        def __exit__(self,*_):events.append(('closed',self.headless))
        def clear_login(self):events.append(('logout',True))
    class Workflow:
        def __init__(self,opened_store,source):self.catalog=ConnectionCatalog(opened_store)
        def observe(self,*,interactive,**_):
            assert interactive
            self.catalog.record(self_account(account_payload()),expected_version=self.catalog.status()['version'])
    monkeypatch.setattr('collection_context.application.connection_runner.BrowserSession',Browser)
    monkeypatch.setattr('collection_context.application.connection_runner.DouyinBrowserSource',lambda b:SimpleNamespace(browser=b))
    monkeypatch.setattr('collection_context.application.connection_runner.ConnectionWorkflow',Workflow)
    runner=ConnectionRunner(store.files.root,tmp_path/'browser',headless=False)
    try:
        runner.start(mode='logout',source_confirmed=True)
        assert terminal(runner)['state']=='completed'
        assert catalog.status()['state']=='not_connected' and runner.status()['headless'] is False
        runner.start(mode='login',source_confirmed=True)
        assert terminal(runner)['state']=='completed' and catalog.status()['state']=='verified'
        assert ('browser',True) in events and ('browser',False) in events
        assert store.snapshot()['items']==before and not store.snapshot()['jobs']
    finally:runner.close()


def test_cli_exposes_separate_source_mode_and_defaults_to_auto():
    args=launcher.parser().parse_args(['--no-browser'])
    assert args.no_browser and args.source_connect_headless is None
    args=launcher.parser().parse_args(['--no-browser','--source-connect-headless'])
    assert args.no_browser and args.source_connect_headless is True
