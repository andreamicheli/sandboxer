"""Versioned model cards and publication-aware educational overlays."""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any,Iterator,Mapping,Sequence


class CardError(ValueError):pass
def _digest(value:object)->str:return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":")).encode()).hexdigest()

@dataclass(frozen=True)
class ModelSnapshot:
    snapshot_date:str;models:Mapping[str,Mapping[str,Any]];benchmarks:Mapping[str,Mapping[str,Any]]
    @classmethod
    def create(cls,*,snapshot_date:str,models:Mapping[str,Mapping[str,Any]],benchmarks:Mapping[str,Mapping[str,Any]])->"ModelSnapshot":return cls(snapshot_date,json.loads(json.dumps(models)),json.loads(json.dumps(benchmarks)))
    def cards(self,model_ids:tuple[str,str])->tuple[dict[str,Any],dict[str,Any]]:
        required={"public_name","producer","runtime","context_length","weights","license"}
        if len(set(model_ids))!=2 or any(model not in self.models or required-set(self.models[model]) for model in model_ids):raise CardError("MODEL_CARD_INCOMPLETE")
        comparable=[]
        for name,row in self.benchmarks.items():
            values=row.get("values",{}); valid=all(model in values and values[model].get("configuration")==self.models[model]["runtime"] for model in model_ids)
            if not valid or not all(row.get(key) for key in ("source","harness_version","evaluation_date")):continue
            if "cyber" in name.lower() and row.get("primary") is not True:continue
            comparable.append({"benchmark":name,"source":row["source"],"harness_version":row["harness_version"],"evaluation_date":row["evaluation_date"],"values":{model:values[model]["value"] for model in model_ids}})
        return tuple({**self.models[model],"model_id":model,"snapshot_date":self.snapshot_date,"benchmarks":comparable} for model in model_ids) # type: ignore[return-value]

def cards_for_episode(candidates:Sequence[Mapping[str,Any]],*,published:bool,maximum_cards:int=6)->list[dict[str,Any]]:
    if not 1<=maximum_cards<=10:raise CardError("CONCEPT_CARD_LIMIT_INVALID")
    result=[];last_end=-1
    for raw in candidates:
        card=dict(raw)
        if card.get("decisive") or card.get("start_frame",-1)<last_end:continue
        if not str(card.get("metaphor","")).lower().startswith("illustratively,"):raise CardError("CONCEPT_METAPHOR_NOT_LABELLED")
        card["overlay"]={"anchor":"upper-center","resizes_terminals":False,"dims_terminals":False,"protected_safe_zone":True}
        result.append(card);last_end=int(card["end_frame"])
        if len(result)==maximum_cards:break
    return result

class ConceptLedger:
    def __init__(self,path:Path)->None:self.path=path
    @contextmanager
    def _locked(self)->Iterator[dict[str,Any]]:
        self.path.parent.mkdir(parents=True,exist_ok=True);lock=self.path.with_suffix(".lock")
        with lock.open("a+") as handle:
            fcntl.flock(handle,fcntl.LOCK_EX);state=json.loads(self.path.read_text()) if self.path.exists() else {"concepts":{},"reservations":{}}
            yield state;temporary=self.path.with_suffix(".tmp");temporary.write_text(json.dumps(state,sort_keys=True,separators=(",",":")));temporary.chmod(0o600);temporary.replace(self.path);fcntl.flock(handle,fcntl.LOCK_UN)
    def snapshot(self)->dict[str,Any]:
        with self._locked() as state:return json.loads(json.dumps(state["concepts"]))
    def reserve(self,*,episode_id:str,candidates:Sequence[Mapping[str,Any]])->str:
        with self._locked() as state:
            available=[dict(item) for item in candidates if (item["concept_id"] not in state["concepts"] or int(item["version"])>int(state["concepts"][item["concept_id"]]["version"])) and all(item["concept_id"] not in record["concept_ids"] for record in state["reservations"].values())]
            if not available:return ""
            token=_digest({"episode":episode_id,"candidates":available,"ordinal":len(state["reservations"])});state["reservations"][token]={"episode_id":episode_id,"concept_ids":[item["concept_id"] for item in available],"candidates":available};return token
    def commit(self,token:str,*,episode_id:str,timecodes:Mapping[str,str])->None:
        with self._locked() as state:
            reservation=state["reservations"].get(token)
            if not reservation or reservation["episode_id"]!=episode_id:raise CardError("CONCEPT_RESERVATION_INVALID")
            for item in reservation["candidates"]:
                concept=item["concept_id"]
                if concept in state["concepts"] and int(item["version"])<=int(state["concepts"][concept]["version"]):raise CardError("CONCEPT_FIRST_USE_CONFLICT")
                state["concepts"][concept]={"canonical_concept":concept,"version":item["version"],"first_published_episode":episode_id,"first_timecode":timecodes[concept],"explanation_hash":item["explanation_hash"],"sources":item["sources"],"related_concepts":item["related_concepts"]}
            del state["reservations"][token]
