-- City registry (UTM SRID per city for buffers) and the abbreviation table used by the text normalizer.

CREATE TABLE IF NOT EXISTS geo.city (
  city_id  SMALLINT PRIMARY KEY,
  name     TEXT NOT NULL,
  utm_srid INT NOT NULL,
  bbox     GEOMETRY(POLYGON, 4326) NOT NULL
);
INSERT INTO geo.city VALUES
 (1, 'Hyderabad', 32644, ST_MakeEnvelope(78.20, 17.20, 78.75, 17.65, 4326)),
 (2, 'Bengaluru', 32643, ST_MakeEnvelope(77.35, 12.75, 77.85, 13.20, 4326))
ON CONFLICT (city_id) DO NOTHING;

-- Abbreviations are expanded with word boundaries, longest first. Keep entries lowercase.
-- This is a starter set; the review queue adds to it from DLQ misses.
CREATE TABLE IF NOT EXISTS geo.abbreviation (
  city_id SMALLINT NOT NULL DEFAULT 0,              -- 0 = every city
  short   TEXT NOT NULL,
  long    TEXT NOT NULL,
  PRIMARY KEY (city_id, short)
);
INSERT INTO geo.abbreviation (city_id, short, long) VALUES
 (0,'jn','junction'), (0,'jnc','junction'), (0,'junc','junction'), (0,'rd','road'), (0,'x roads','cross roads'),
 (0,'x road','cross roads'), (0,'xroads','cross roads'), (0,'u/p','underpass'), (0,'f/o','flyover'), (0,'fo','flyover'),
 (0,'twds','towards'), (0,'thru','through'), (0,'bw','between'), (0,'b/w','between'), (0,'nr','near'),
 (0,'stn','station'), (0,'sig','signal'), (0,'circ','circle'), (0,'blr','bengaluru'), (0,'hyd','hyderabad'),
 (0,'orr','outer ring road'), (0,'nh','national highway'), (0,'sh','state highway'), (0,'rob','rail over bridge'),
 (0,'rub','rail under bridge'), (0,'veh','vehicles'), (0,'traf','traffic'), (0,'ps','police station'),
 (1,'pvnr','pv narasimha rao expressway'), (1,'mgbs','mahatma gandhi bus station'), (1,'jbs','jubilee bus station'),
 (1,'kphb','kukatpally housing board'), (1,'lb nagar','l b nagar'), (1,'sr nagar','s r nagar'), (1,'ecil','ecil'),
 (1,'hitec','hitec city'), (1,'hitech city','hitec city'), (1,'gachi','gachibowli'), (1,'madhapur','madhapur'),
 (1,'bhel','bhel'), (1,'rgia','rajiv gandhi international airport'), (1,'airport','rajiv gandhi international airport'),
 (1,'secbad','secunderabad'), (1,'sec bad','secunderabad'), (1,'tank bund','tank bund road'), (1,'nec','necklace road'),
 (1,'tarnaka','tarnaka'), (1,'uppal','uppal'), (1,'chaderghat','chaderghat'), (1,'nampally','nampally'),
 (1,'abids','abids'), (1,'koti','koti'), (1,'mehdipatnam','mehdipatnam'), (1,'toli chowki','tolichowki')
ON CONFLICT DO NOTHING;
