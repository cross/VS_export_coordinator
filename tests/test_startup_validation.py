"""Status help is visible before current-run validation starts."""
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
from contextlib import nullcontext
import unittest
from unittest.mock import patch, Mock

import vintage_story_export_coordinator as c


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.cuboids=[c.Cuboid(1,0,1,65,66,0,1,'s00001')]

    def test_main_prints_signal_help_before_starting_isolated_run_collection(self):
        region=self.cuboids[0]
        plan={'cuboids':[{'index':region.index,'name':region.name,**c.capture_plan.box(vars(region))}],
              'requests':[c.capture_plan.box(vars(region))],'prefix':'v12_capture_test','plan_hash':'test',
              'geometry_hash':'geometry','geometry_version':'test','requested_y':{'ymin':65,'ymax':66},
              'partition':{'x_span':2,'z_span':2}}
        output=io.StringIO()
        class StopAfterBanner(Exception):
            pass
        def before_check(*args,**kwargs):
            self.assertIn('kill -USR1',output.getvalue())
            self.assertIn('Run ID:',output.getvalue())
            raise StopAfterBanner
        log=self.root/'server.log'; log.touch()
        unit={'id':'s00001','index':1,'core':c.capture_plan.box(vars(region)),'capture':c.capture_plan.box(vars(region))}
        run=SimpleNamespace(state={'run_id':'test','operational_model':c.capture_run.OPERATIONAL_MODEL,'prefix':'test',
                                   'units':[unit],'sections':{},'settings':{'overlap':3,'coverage_y':66},
                                   'source':{'server_log':str(log)}},plan=plan,directory=self.root,
                            path=self.root/'test-progress.json',units={unit['id']:unit},export_dir=self.root,
                            collect=Mock(side_effect=before_check),locked=nullcontext)
        args=['coordinator','--plan',str(self.root/'plan.json'),'--target-pane','%1','--export-dir',str(self.root),'--server-log',str(log)]
        with patch('sys.argv',args), patch.object(c.capture_plan,'load_plan',return_value=plan), patch.object(c.capture_run.Run,'create',return_value=run), patch.object(c.capture_run,'register_run'), patch.object(c.capture_run,'discover_runs',return_value=([],[])), patch('builtins.input',return_value=''), patch('sys.stdout',new=output), patch('subprocess.run',side_effect=AssertionError('No server commands')):
            with self.assertRaises(StopAfterBanner):
                c.main()


if __name__=='__main__':
    unittest.main()
