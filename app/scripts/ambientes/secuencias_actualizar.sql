DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT 
            n.nspname AS schema_name,
            c.relname AS table_name,
            a.attname AS column_name,
            pg_get_serial_sequence(
                format('%I.%I', n.nspname, c.relname),
                a.attname
            ) AS sequence_name
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid
        WHERE c.relkind = 'r'
          AND a.attnum > 0
          AND NOT a.attisdropped
    LOOP
        IF r.sequence_name IS NOT NULL THEN
            EXECUTE format(
                'SELECT setval(''%s'', (SELECT COALESCE(MAX(%I), 1) FROM %I.%I), true);',
                r.sequence_name,
                r.column_name,
                r.schema_name,
                r.table_name
            );
        END IF;
    END LOOP;
END$$;