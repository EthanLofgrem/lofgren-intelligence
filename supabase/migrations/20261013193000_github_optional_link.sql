-- Optional, disabled-by-default GitHub identity and explicit repository selection.
-- Backend-only: authenticated subject MUST come from verified LI auth, not input.
CREATE TABLE public.li_github_link_states (
 state_hash text PRIMARY KEY CHECK (state_hash ~ '^[a-f0-9]{64}$'),
 subject uuid NOT NULL, consumed boolean NOT NULL DEFAULT false, verifier text NOT NULL, callback text NOT NULL,
 expires_at timestamptz NOT NULL DEFAULT (clock_timestamp() + interval '5 minutes')
);
CREATE TABLE public.li_github_connections (
 subject uuid PRIMARY KEY, github_user_id bigint UNIQUE NOT NULL CHECK(github_user_id > 0),
 installation_id bigint CHECK(installation_id > 0), repository_ids bigint[] NOT NULL DEFAULT '{}',
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE public.li_github_link_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.li_github_connections ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.li_github_link_states, public.li_github_connections FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TYPE public.li_github_link_states, public.li_github_connections FROM PUBLIC, anon, authenticated;
GRANT USAGE ON TYPE public.li_github_link_states, public.li_github_connections TO service_role;

CREATE FUNCTION public.li_github_begin_link(p_subject uuid,p_state_hash text,p_verifier text,p_callback text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
 IF p_subject IS NULL OR p_verifier IS NULL OR p_callback IS NULL OR p_state_hash IS NULL OR length(p_verifier) NOT BETWEEN 43 AND 128 OR p_callback !~ '^https://' THEN RAISE EXCEPTION 'invalid link request'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(p_subject::text,712));
 DELETE FROM li_github_link_states WHERE subject=p_subject OR expires_at<=clock_timestamp();
 INSERT INTO li_github_link_states(state_hash,subject,verifier,callback) VALUES(p_state_hash,p_subject,p_verifier,p_callback);
 RETURN true;
END $$;
CREATE FUNCTION public.li_github_consume_link(p_subject uuid,p_state_hash text,p_callback text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=public AS $$
DECLARE v_verifier text;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended(p_subject::text,712));
 UPDATE li_github_link_states SET consumed=true WHERE state_hash=p_state_hash AND subject=p_subject AND callback=p_callback AND NOT consumed AND expires_at>clock_timestamp() RETURNING verifier INTO v_verifier;
 IF v_verifier IS NULL THEN RETURN NULL; END IF;
 RETURN jsonb_build_object('verifier',v_verifier);
END $$;
CREATE FUNCTION public.li_github_link_identity(p_subject uuid,p_github_user_id bigint,p_state_hash text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=public AS $$
BEGIN
 IF p_subject IS NULL OR p_github_user_id IS NULL OR p_github_user_id<=0 THEN RETURN false; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(p_subject::text,712));
 IF NOT EXISTS(SELECT 1 FROM li_github_link_states WHERE state_hash=p_state_hash AND subject=p_subject AND consumed AND expires_at>clock_timestamp()) THEN RETURN false; END IF;
 DELETE FROM li_github_link_states WHERE state_hash=p_state_hash AND subject=p_subject;
 IF EXISTS(SELECT 1 FROM li_github_connections WHERE subject=p_subject AND github_user_id<>p_github_user_id) THEN RETURN false; END IF;
 INSERT INTO li_github_connections(subject,github_user_id) VALUES(p_subject,p_github_user_id) ON CONFLICT(subject) DO NOTHING;
 RETURN true;
EXCEPTION WHEN unique_violation THEN RETURN false;
END $$;
CREATE FUNCTION public.li_github_select_repositories(p_subject uuid,p_github_user_id bigint,p_installation_id bigint,p_repository_ids bigint[])
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=public AS $$
BEGIN
 IF p_installation_id IS NULL OR p_installation_id<=0 OR p_repository_ids IS NULL OR cardinality(p_repository_ids) NOT BETWEEN 1 AND 100 OR cardinality(p_repository_ids)<>(SELECT count(DISTINCT r) FROM unnest(p_repository_ids) r) OR EXISTS(SELECT 1 FROM unnest(p_repository_ids) r WHERE r IS NULL OR r<=0) THEN RETURN false; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(p_subject::text,712));
 UPDATE li_github_connections SET installation_id=p_installation_id,repository_ids=p_repository_ids,updated_at=clock_timestamp() WHERE subject=p_subject AND github_user_id=p_github_user_id;
 RETURN FOUND;
END $$;
CREATE FUNCTION public.li_github_connection_status(p_subject uuid)
RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=public AS $$
 SELECT coalesce((SELECT jsonb_build_object('connected',true,'github_user_id',github_user_id,'installation_id',installation_id,'repository_ids',repository_ids) FROM li_github_connections WHERE subject=p_subject),'{"connected":false}'::jsonb)
$$;
CREATE FUNCTION public.li_github_disconnect(p_subject uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=public AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended(p_subject::text,712));
 DELETE FROM li_github_link_states WHERE subject=p_subject;
 DELETE FROM li_github_connections WHERE subject=p_subject;
 RETURN true;
END $$;
REVOKE ALL ON FUNCTION public.li_github_begin_link(uuid,text,text,text),public.li_github_consume_link(uuid,text,text),public.li_github_link_identity(uuid,bigint,text),public.li_github_select_repositories(uuid,bigint,bigint,bigint[]),public.li_github_connection_status(uuid),public.li_github_disconnect(uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.li_github_begin_link(uuid,text,text,text),public.li_github_consume_link(uuid,text,text),public.li_github_link_identity(uuid,bigint,text),public.li_github_select_repositories(uuid,bigint,bigint,bigint[]),public.li_github_connection_status(uuid),public.li_github_disconnect(uuid) TO service_role;
