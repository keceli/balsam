from django.shortcuts import render, redirect
from balsam.core.models import BalsamJob
from django.http import JsonResponse, HttpResponseBadRequest

def list_tasks(request):
    if not request.is_ajax():
        return HttpResponseBadRequest()
    
    #print("Parsed GET dict:")
    #print('\n'.join(f"{p} == {request.GET[p]}" for p in request.GET))

    _jobs = BalsamJob.objects.values_list('job_id', 'name', 'workflow',
                                          'application', 'state',
                                          'queued_launch__scheduler_id',
                                          'num_nodes', 'ranks_per_node')
    return JsonResponse({"data": list(_jobs)})
