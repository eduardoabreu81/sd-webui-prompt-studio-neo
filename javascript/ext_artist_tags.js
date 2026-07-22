// Artist (category 1) completion via the '@' trigger. The optional prefix on
// insertion is handled centrally so it composes with normal tag sanitization.
const ARTIST_TAG_REGEX = /^@[^,\s]*/;
const ARTIST_TAG_TRIGGER = () => TAC_CFG.artistAtTrigger && tagword.match(ARTIST_TAG_REGEX);

class ArtistTagParser extends BaseTagParser {
    parse() {
        const searchTerm = tagword.slice(1);
        let tempResults = allTags.filter(t => parseInt(t[1]) === 1);

        if (searchTerm.length > 0) {
            const regex = new RegExp(escapeRegExp(searchTerm, true), 'i');
            const searchByAlias = TAC_CFG.alias.searchByAlias;
            tempResults = tempResults.filter(t =>
                regex.test(t[0].toLowerCase()) ||
                regex.test(t[0].toLowerCase().replaceAll(" ", "_")) ||
                (searchByAlias && t[3] && regex.test(t[3].toLowerCase()))
            );
        }

        return tempResults.map(t => {
            const result = new AutocompleteResult(t[0].trim(), ResultType.tag);
            result.category = parseInt(t[1]);
            result.count = t[2];
            return result;
        });
    }
}

PARSERS.push(new ArtistTagParser(ARTIST_TAG_TRIGGER));
