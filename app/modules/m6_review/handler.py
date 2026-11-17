from flask import Blueprint,request,jsonify
from app.pipeline import run
from app import store
from app.errors import DomainError
from app.modules.m6_review import revision_service as svc
bp=Blueprint("m6",__name__)
@bp.route("/api/m6",methods=["GET","POST"])
def endpoint():
    payload=request.get_json(silent=True) if request.method=="POST" else {}
    if not isinstance(payload,dict): raise DomainError("BODY","请求必须是对象")
    raw=payload.get("text")
    if raw is not None and not isinstance(raw,str): raise DomainError("TEXT","数据必须为文本")
    return jsonify(run(raw,payload.get("params"),stop=6,persist=False))
@bp.get("/api/revision/jobs")
def jobs():
    return jsonify(store.list_jobs())
@bp.post("/api/revision/<run_id>")
def submit(run_id):
    payload=request.get_json(silent=True)
    if not isinstance(payload,dict): raise DomainError("BODY","请求必须是对象")
    text=payload.get("text")
    if not isinstance(text,str) or not text.strip(): raise DomainError("TEXT","补传数据必须为文本")
    return jsonify(svc.submit_revision(run_id,text,payload.get("params")))
@bp.get("/api/revision/job/<job_id>")
def detail(job_id):
    return jsonify(store.get_job(job_id))
@bp.post("/api/revision/job/<job_id>/adjudicate")
def adjudicate(job_id):
    payload=request.get_json(silent=True)
    if not isinstance(payload,dict): raise DomainError("BODY","请求必须是对象")
    return jsonify(svc.adjudicate(job_id,payload.get("station"),payload.get("time"),payload.get("ref",payload.get("line"))))
@bp.post("/api/revision/job/<job_id>/review")
def review_candidate(job_id):
    payload=request.get_json(silent=True)
    if not isinstance(payload,dict): raise DomainError("BODY","请求必须是对象")
    result,job=svc.review_candidate(job_id,payload.get("version"),payload.get("decision"))
    return jsonify({"job":job,"candidate":result})
@bp.post("/api/revision/job/<job_id>/switch")
def switch(job_id):
    payload=request.get_json(silent=True) or {}
    expected=payload.get("expected_active")
    job,previous=svc.switch(job_id,expected)
    return jsonify({"job":job,"previous_active":previous,"active_run_id":store.active()})
@bp.get("/api/active")
def active():
    run_id=store.active()
    return jsonify({"active_run_id":run_id,"ledger":store.fetch(run_id) if run_id else None})
