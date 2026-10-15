import io
import json
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import ADB, Store, Studio, UserError, create_server, nodes_from_xml, selector_at

class FakeADB:
    def __init__(self):
        self.commands=[]
        self.width=1080
        self.height=2400
        self.nodes=[{'resource-id':'com.example:id/open','text':'Open','class':'android.widget.Button','bounds':'[10,10][110,110]','focused':'false'},
                    {'resource-id':'com.example:id/email','text':'','class':'android.widget.EditText','bounds':'[10,200][800,300]','focused':'true'}]
    def devices(self): return [{'serial':'test-phone','state':'device','model':'Fake phone'}]
    def validate_device(self, serial):
        if serial!='test-phone': raise UserError('Device not ready')
    def launch(self, serial, package): self.commands.append(('launch',package))
    def frame(self, serial): return {'width':self.width,'height':self.height,'image':''}
    def hierarchy(self, serial): return self.nodes
    def shell(self, serial,*args): self.commands.append(args); return ''
    def text(self, serial, value): self.commands.append(('text', value))

class StudioTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.adb=FakeADB()
        self.studio=Studio(Store(Path(self.tmp.name)/'db.sqlite'),self.adb)
        self.project=self.studio.project_create({'name':'Test app','package':'com.example.app'})['id']
    def tearDown(self):
        self.studio.cancel.set()
        deadline=time.monotonic()+3
        while self.studio.active_run and time.monotonic()<deadline: time.sleep(.01)
        self.tmp.cleanup()
    def start(self): self.studio.start_recording({'project':self.project,'serial':'test-phone'})
    def action(self, **data): return self.studio.action({'serial':'test-phone',**data})
    def save(self): return self.studio.save_recording({'name':'Journey'})['id']
    def finish(self, run):
        deadline=time.monotonic()+5
        while self.studio.active_run and time.monotonic()<deadline: time.sleep(.01)
        self.assertIsNone(self.studio.active_run)
        return next(r for r in self.studio.state()['runs'] if r['id']==run)
    def test_record_save_replay_and_variables_are_not_persisted(self):
        self.start()
        self.action(action='tap',x=50/1080,y=50/2400)
        self.action(action='text',variable='LOGIN_EMAIL',value='secret-for-recording')
        self.action(action='assert',text='Open')
        ident=self.save()
        saved=self.studio.store.flow(ident)
        self.assertEqual(saved['steps'][1]['selector'],{'resource-id':'com.example:id/open'})
        self.assertNotIn('secret-for-recording',json.dumps(self.studio.state()))
        # Locator moves: replay must tap its new center, not the recorded coordinates.
        self.adb.nodes[0]['bounds']='[200,400][400,600]'
        run=self.studio.start_run({'flow':ident,'serial':'test-phone','variables':{'LOGIN_EMAIL':'secret-for-run'}})['id']
        result=self.finish(run)
        self.assertEqual(result['status'],'passed')
        self.assertIn(('input','tap',300,500),self.adb.commands)
        self.assertNotIn('secret-for-run',json.dumps(self.studio.state()))
        self.assertEqual(len(result['results']),4)
        self.assertEqual(len(Studio(Store(Path(self.tmp.name)/'db.sqlite'),self.adb).state()['flows']),1)
    def test_ambiguous_selector_stops_without_tapping(self):
        self.start();self.action(action='tap',x=50/1080,y=50/2400);ident=self.save()
        self.adb.nodes.append(dict(self.adb.nodes[0]));self.adb.commands=[]
        run=self.studio.start_run({'flow':ident,'serial':'test-phone'})['id']
        result=self.finish(run)
        self.assertEqual(result['status'],'failed')
        self.assertIn('several elements',result['error'])
        self.assertFalse(any(c[:2]==('input','tap') for c in self.adb.commands))
    def test_coordinate_opt_in_and_orientation_guard(self):
        self.start();self.action(action='tap',x=.9,y=.9);ident=self.save()
        with self.assertRaisesRegex(UserError,'coordinate'): self.studio.start_run({'flow':ident,'serial':'test-phone'})
        self.adb.width=2400;self.adb.height=1080
        run=self.studio.start_run({'flow':ident,'serial':'test-phone','allow_coordinates':True})['id']
        result=self.finish(run)
        self.assertEqual(result['status'],'failed')
        self.assertIn('orientation',result['error'])
    def test_missing_assertion_is_not_recorded(self):
        self.start()
        with self.assertRaisesRegex(UserError,'not currently visible'): self.action(action='assert',text='Never appeared')
        self.assertEqual(len(self.studio.recording['steps']),1)
    def test_cancel_stops_before_later_steps(self):
        self.start();self.action(action='wait',seconds=20);self.action(action='key',key='BACK');ident=self.save()
        self.adb.commands=[]
        run=self.studio.start_run({'flow':ident,'serial':'test-phone'})['id']
        time.sleep(.1);self.studio.cancel.set()
        self.assertEqual(self.finish(run)['status'],'cancelled')
        self.assertNotIn(('input','keyevent','KEYCODE_BACK'),self.adb.commands)
    def test_text_target_must_be_focused(self):
        self.start();self.action(action='text',variable='EMAIL',value='test');ident=self.save()
        self.adb.nodes[1]['focused']='false';self.adb.commands=[]
        run=self.studio.start_run({'flow':ident,'serial':'test-phone','variables':{'EMAIL':'other'}})['id']
        self.assertEqual(self.finish(run)['status'],'failed')
        self.assertNotIn(('text','other'),self.adb.commands)
    def test_ai_receives_structure_only(self):
        self.start();self.action(action='tap',x=50/1080,y=50/2400);self.action(action='assert',text='Open');ident=self.save()
        captured=[]
        class Response(io.BytesIO): pass
        def provider(request,timeout):
            captured.append(json.loads(request.data))
            return Response(json.dumps({'choices':[{'message':{'content':'Add a negative case.'}}]}).encode())
        with patch.dict('os.environ',{'FLOWPILOT_AI_BASE_URL':'http://127.0.0.1:9999/v1','FLOWPILOT_AI_MODEL':'example'}),patch('urllib.request.urlopen',provider):
            result=self.studio.review(ident)
        self.assertEqual(result['review'],'Add a negative case.')
        sent=json.dumps(captured)
        self.assertNotIn('com.example',sent)
        self.assertNotIn('Open',sent)
        self.assertNotIn('Journey',sent)
        self.assertIn('selector_fields',sent)
    def test_duplicate_ids_use_unique_text(self):
        nodes=nodes_from_xml('<hierarchy><node resource-id="id/row" text="A" bounds="[0,0][20,20]"/><node resource-id="id/row" text="B" bounds="[0,30][20,50]"/></hierarchy>')
        self.assertEqual(selector_at(nodes,10,40),{'resource-id':'id/row','text':'B'})
    def test_remote_shell_arguments_are_quoted(self):
        adb=ADB();calls=[]
        adb.command=lambda args, serial, timeout: calls.append(args) or b''
        value="a'; touch /tmp/not-executed; # $(id)"
        adb.text('test-phone',value)
        command=shlex.split(calls[0][1])
        self.assertEqual(command,['input','text',value.replace(' ','%s')])
    def test_rejects_invalid_input(self):
        with self.assertRaises(UserError): self.studio.project_create({'name':'Bad','package':'com.example;id'})
        with self.assertRaises(UserError): ADB().text('test-phone','100%')
        with self.assertRaises(UserError): ADB().text('test-phone','தமிழ்')

class HTTPTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.server=create_server(0,Path(self.tmp.name),FakeADB())
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_address[1])
    def tearDown(self): self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
    def req(self,path,headers=None,data=None):
        request=urllib.request.Request(self.base+path,headers=headers or {},data=json.dumps(data).encode() if data is not None else None)
        try:
            with urllib.request.urlopen(request) as r:return r.status,r.read()
        except urllib.error.HTTPError as e:return e.code,e.read()
    def test_host_origin_and_token_guards(self):
        self.assertEqual(self.req('/api/state')[0],400)
        h={'X-Flowpilot-Token':self.server.token,'Content-Type':'application/json'}
        self.assertEqual(self.req('/api/state',h)[0],200)
        self.assertEqual(self.req('/api/state',{**h,'Origin':'https://evil.example'})[0],400)
        self.assertEqual(self.req('/api/state',{**h,'Host':'evil.example'})[0],400)
        self.assertEqual(self.req('/api/projects',h,{'name':'App','package':'com.example.app'})[0],200)
        self.assertEqual(self.req('/api/action',h,{'action':'arbitrary_shell','serial':'test-phone'})[0],400)
    def test_serves_ui_and_reports_missing_device(self):
        status,html=self.req('/')
        self.assertEqual(status,200);self.assertNotIn(b'__TOKEN__',html)
        self.assertIn(self.server.token.encode(),html)
        self.assertEqual(self.req('/app.js')[0],200)
        self.assertEqual(self.req('/style.css')[0],200)
        self.assertEqual(self.req('/../../server.py')[0],404)
        h={'X-Flowpilot-Token':self.server.token,'Content-Type':'application/json'}
        status,result=self.req('/api/devices',h)
        self.assertEqual(status,200);self.assertEqual(json.loads(result)['devices'][0]['serial'],'test-phone')
    def test_malformed_json_is_error_not_crash(self):
        request=urllib.request.Request(self.base+'/api/projects',data=b'{',headers={'X-Flowpilot-Token':self.server.token,'Content-Type':'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(request)
        self.assertEqual(error.exception.code,400)

if __name__=='__main__':unittest.main()
