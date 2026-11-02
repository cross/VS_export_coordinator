"""Acknowledgements must belong to the newly issued console command."""
from pathlib import Path
import tempfile
import unittest

from server_console import Console
from capture_run import chunk_bounds


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.log=self.root/'server.log'; self.log.touch()

    def test_sends_selected_tmux_and_pane_and_records_current_response(self):
        calls=[]
        def send(path,pane,binary):
            command=path.read_text().strip()
            calls.append((command,pane,binary))
            with self.log.open('a') as stream:
                stream.write('Handling Console Command '+command+'\nStart and end position marked\n')
        console=Console('/custom/tmux','%9',self.log,self.root/'commands',send)
        command='/executeas Player /we select 0 65 0 1 180 1'
        response=console.command(command,'Start and end position marked')
        self.assertEqual(calls,[(command,'%9','/custom/tmux')])
        self.assertIn('Start and end position marked',response)
        self.assertTrue((self.root/'commands'/'0001-response.txt').is_file())

    def test_acknowledgement_from_before_issuance_does_not_count(self):
        self.log.write_text('Handling Console Command /test\nready\n')
        console=Console('tmux','%1',self.log,self.root/'commands',lambda *args:None)
        with self.assertRaisesRegex(TimeoutError,'No expected acknowledgement'):
            console.command('/test','ready',timeout=.001)

    def test_server_rejection_does_not_count_as_success(self):
        def send(path,*args):
            with self.log.open('a') as stream:
                stream.write('Handling Console Command '+path.read_text().strip()+'\nNo such player\n')
        console=Console('tmux','%1',self.log,self.root/'commands',send)
        with self.assertRaisesRegex(RuntimeError,'No such player'):
            console.command('/test','ready')

    def test_deep_probe_chunks_cover_negative_coordinates_and_obey_limits(self):
        regions=[dict(xmin=-33,xmax=-1,zmin=-1,zmax=0),dict(xmin=-32,xmax=-30,zmin=-1,zmax=0)]
        probes=list(chunk_bounds(regions,0,180))
        self.assertEqual({(b['xmin'],b['zmin']) for b in probes},{(-64,-32),(-64,0),(-32,-32),(-32,0)})
        with self.assertRaisesRegex(ValueError,'selection limits'):
            list(chunk_bounds(regions,0,1023))


if __name__=='__main__':
    unittest.main()
