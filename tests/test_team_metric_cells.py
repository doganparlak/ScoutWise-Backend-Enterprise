import json
import os
import sys
import types
import unittest
from unittest.mock import patch

from report_module.metric_cells import bind_metric_cells
from match_report_module.report import build_team_report_strengths, build_team_report_weaknesses, build_team_report_attack_profile, build_team_report_defense_profile


class MetricCellTests(unittest.TestCase):
    def test_values_are_taken_from_their_matching_labels(self):
        evidence={'Free Kicks':{'label':'Free Kicks','value':'13.2'},'Expected Goals Free Kicks (xGFK)':{'label':'Expected Goals Free Kicks (xGFK)','value':'0.03'}}
        selected=[{'label':'Free Kicks','value':'0.03'},{'label':'Expected Goals Free Kicks (xGFK)','value':'13.2'}]
        result=bind_metric_cells(selected,evidence,4)
        self.assertEqual(result,list(evidence.values()))
    def test_unavailable_and_duplicate_metrics_are_rejected(self):
        evidence={'Free Kicks':{'label':'Free Kicks','value':'13.2'}}
        result=bind_metric_cells([{'label':'xGFK','value':99},{'label':'Free Kicks','value':99},{'label':'Free Kicks','value':99}],evidence,4)
        self.assertEqual(result,[evidence['Free Kicks']])
    def test_malformed_selection_and_missing_metrics_are_safe(self):
        self.assertEqual(bind_metric_cells(None,{},4),[])
        self.assertEqual(bind_metric_cells(['Free Kicks',{}],{},4),[])
    def test_binding_does_not_mutate_evidence(self):
        evidence={'Fouls':{'label':'Fouls','value':'10.4'}}
        result=bind_metric_cells([{'label':'Fouls'}],evidence,4)
        result[0]['value']='999'
        self.assertEqual(evidence['Fouls']['value'],'10.4')
    def test_generated_strengths_and_weaknesses_use_actual_team_values(self):
        names={'Fouls':10.4,'Yellow Cards':1.8,'Offsides':2.1,'Free Kicks':13.2}
        metrics={'errors_discipline':[{'name':name,'perMatch':value,'aggregation':'total'} for name,value in names.items()]}
        reports=[{'teams':[{'id':3853,'name':'Sarıyer SK'}]}]
        generated={'themes':[{'title':'Test theme','metrics':[{'label':name,'value':'999'} for name in names],'analysis':['Source-backed analysis.','Additional source-backed analysis.']} for _ in range(3)]}
        class Model:
            def __init__(self,**kwargs):pass
            def invoke(self,*args,**kwargs):return types.SimpleNamespace(content=json.dumps(generated))
        with patch.dict(os.environ,{'OPENAI_API_KEY':'offline-test'}),patch.dict(sys.modules,{'langchain_openai':types.SimpleNamespace(ChatOpenAI=Model)}):
            for builder,args in [(build_team_report_strengths,(reports,3853,metrics,'en')),(build_team_report_weaknesses,(reports,3853,metrics,{},'en'))]:
                result=builder(*args)
                self.assertEqual(len(result['themes']),3)
                for theme in result['themes']:
                    for metric in theme['metrics']:
                        self.assertEqual(float(metric['value']),names[metric['label']])
                        self.assertNotIn('xG',metric['label'])

    def test_attack_and_defense_bind_metrics_to_their_own_evidence(self):
        cases=[
            (build_team_report_attack_profile,'shooting',{'Goals':1.3,'Shots Total':12,'Shots On Target':3.5,'Shots Off Target':5.1}),
            (build_team_report_defense_profile,'defending',{'Tackles':10,'Interceptions':6,'Clearances':20,'Duels Won':40}),
        ]
        for builder,group,values in cases:
            generated={'themes':[{'title':'Test theme','metrics':[{'label':name,'value':'999'} for name in values],'analysis':['Source-backed analysis.','Additional source-backed analysis.']} for _ in range(3)],'players':[]}
            class Model:
                def __init__(self,**kwargs):pass
                def invoke(self,*args,**kwargs):return types.SimpleNamespace(content=json.dumps(generated))
            reports=[{'teams':[{'id':3853,'name':'Sarıyer SK'}],'lineups':[]}]
            metrics={group:[{'name':name,'perMatch':value,'aggregation':'total'} for name,value in values.items()]}
            with patch.dict(os.environ,{'OPENAI_API_KEY':'offline-test'}),patch.dict(sys.modules,{'langchain_openai':types.SimpleNamespace(ChatOpenAI=Model)}):
                result=builder(reports,3853,metrics,'en')
                self.assertEqual(len(result['themes']),3)
                for theme in result['themes']:
                    for metric in theme['metrics']:
                        self.assertEqual(float(metric['value']),values[metric['label']])

if __name__=='__main__':unittest.main()
