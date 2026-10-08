'use strict';

const vertexScope = "g.V().has('account_ref', account).has('namespace', namespace).has('generation', generation)";
const metadata = ".property('account_ref', account).property('namespace', namespace).property('generation', generation)";
const payload = ".property('logical_id', logical).property('transport_json', transport)";
const projection = ".project('transport','physical','label','account','namespace','generation','logical','source','target','index','sent','conversation')" +
  ".by('transport_json').by(__.id()).by(__.label()).by('account_ref').by('namespace').by('generation').by('logical_id')";
const vertexProjection = projection + ".by(__.constant('')).by(__.constant('')).by('record_index').by('sent_us').by('conversation_ref')";
const edgeProjection = projection + ".by(__.outV().id()).by(__.inV().id()).by(__.constant(0)).by(__.constant(0)).by(__.constant(''))";
const page = ".order().by(__.select('logical'), incr).limit(limit)";

module.exports = Object.freeze({
  put_vertex: "g.V([account, physical]).fold().coalesce(unfold(), addV(label).property('id', physical)" +
    metadata + payload + ".property('record_index', recordIndex).property('sent_us', sentUs)" +
    ".property('conversation_ref', conversation)).values('transport_json')",
  put_edge: "g.V([account, source]).has('namespace', namespace).has('generation', generation)" +
    ".outE(label).hasId(physical).fold().coalesce(unfold(), __.V([account, source])" +
    ".has('namespace', namespace).has('generation', generation).addE(label)" +
    ".to(__.V([account, target]).has('namespace', namespace).has('generation', generation))" +
    ".property('id', physical)" + metadata + payload + ").values('transport_json')",
  read_records: vertexScope + ".union(__.has('transport_json').has('logical_id', gt(after))" + vertexProjection +
    ", __.outE().has('account_ref', account).has('namespace', namespace).has('generation', generation)" +
    ".has('transport_json').has('logical_id', gt(after))" + edgeProjection + ")" + page,
  no_later_creator_reply: vertexScope + ".hasLabel('exchange_observation')" +
    ".has('sent_us', gt(retention)).has('sent_us', lte(cutoff))" +
    ".has('logical_id', gt(after))" + vertexProjection + page,
  no_later_creator_reply_filtered: vertexScope + ".hasLabel('exchange_observation')" +
    ".has('conversation_ref', conversation).has('sent_us', gt(retention)).has('sent_us', lte(cutoff))" +
    ".has('logical_id', gt(after))" + vertexProjection + page,
  pricing_discussions: vertexScope + ".hasLabel('exchange_observation')" +
    ".has('sent_us', gt(retention)).has('sent_us', gte(start)).has('sent_us', lt(end))" +
    ".has('sent_us', lte(cutoff)).has('logical_id', gt(after))" +
    vertexProjection + page,
  pricing_discussions_filtered: vertexScope + ".hasLabel('exchange_observation')" +
    ".has('conversation_ref', conversation).has('sent_us', gt(retention))" +
    ".has('sent_us', gte(start)).has('sent_us', lt(end)).has('sent_us', lte(cutoff))" +
    ".has('logical_id', gt(after))" + vertexProjection + page,
  publish: "g.V([account, physical]).fold().coalesce(unfold(), addV('exchange_publication')" +
    ".property('id', physical)" + metadata + ".property('manifest_json', manifest)).values('manifest_json')",
  read_manifest: "g.V([account, physical]).has('namespace', namespace).has('generation', generation).values('manifest_json')",
  cleanup: vertexScope + ".drop()",
});
