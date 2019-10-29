/**
 * Header Search query from component
 * @module ts/components/header/HeaderSearchForm
 */

import Collapsable from 'ts/components/generic/Collapsable';
import History from 'history';
import React from 'react';
import SearchBarInput from 'ts/components/header/SearchBarInput';
import SearchOptionsCollapseToggle from
    'ts/components/header/SearchOptionsCollapseToggle';
import SearchOptionsInput from 'ts/components/header/SearchOptionsInput';

import {
    AllNullable,
    SearchOptions,
    isSearchOption,
} from 'ts/types/types';
import {
    applyDefaultSearchOptions,
    getSearchOptionsFromLocation,
    getSearchUrl,
    loadUserSearchOptions,
    setUserSearchOption,
} from 'ts/app/search';

interface HeaderSearchFormProps {
    searchQuery: string;
    loadingSearch: boolean;
    onSearchQueryChange: (newValue: string) => void;
    location: History.Location;
    history: History.History;
}
type Props = HeaderSearchFormProps;

interface HeaderSearchFormState {
    options: SearchOptions;
    errorValueSubmitted: boolean;
    optionsCollapsed: boolean;
    optionsCollapseAnimating: boolean;
    optionsCollapseAnimateEnabled: boolean;
}
type State = HeaderSearchFormState;

const MAX_QUERY_LENGTH = 100;


function logNoIndexedDbWarning(): void {
    console.warn(
        'Unable to use IndexedDB, so user-specified search options will not '
        + 'be stored across sessions.'
    );
}

class HeaderSearchForm extends React.Component<Props, State> {
    private _defaultSearchOptionUsed: Set<keyof SearchOptions>;
    private _historyUnlistenCallback: History.UnregisterCallback | null;

    constructor(props: Props) {
        super(props);
        this.bindEventHandlers();
        this._defaultSearchOptionUsed = new Set<keyof SearchOptions>();
        this._historyUnlistenCallback = null;

        this.state = {
            options: this.getInitSearchOptions(),
            errorValueSubmitted: false,
            optionsCollapsed: true,
            optionsCollapseAnimating: false,
            optionsCollapseAnimateEnabled: false,
        };
    }

    componentDidMount(): void {
        function handleHistoryChange(
            this: HeaderSearchForm, location: History.Location
        ): void {
            if (location.pathname === '/') {
                this.handleInputtedQueryChange('');
            }
            this.handlePageNavigation();
        }
        this._historyUnlistenCallback = this.props.history.listen(
            handleHistoryChange.bind(this)
        );

        if (this._defaultSearchOptionUsed.size > 0) {
            loadUserSearchOptions().then(
                this.handleLoadedUserSearchOptions,
                logNoIndexedDbWarning
            );
        }
    }

    componentWillUnmount(): void {
        if (this._historyUnlistenCallback !== null) {
            this._historyUnlistenCallback();
        }
    }

    bindEventHandlers(): void {
        this.handlePageNavigation = this.handlePageNavigation.bind(this);
        this.handleSubmit = this.handleSubmit.bind(this);
        this.handleInputtedQueryChange = (
            this.handleInputtedQueryChange.bind(this)
        );
        this.handleSearchOptionsChange = (
            this.handleSearchOptionsChange.bind(this)
        );
        this.handleLoadedUserSearchOptions = (
            this.handleLoadedUserSearchOptions.bind(this)
        );
        this.handleSearchOptionsCollapseToggle = (
            this.handleSearchOptionsCollapseToggle.bind(this)
        );
        this.handleSearchOptionsCollapseAnimationEnd = (
            this.handleSearchOptionsCollapseAnimationEnd.bind(this)
        );
    }

    getInitSearchOptions(): SearchOptions {
        const locationOptions = getSearchOptionsFromLocation(
            this.props.location
        );
        for (const optionKey of Object.keys(locationOptions)) {
            if (!isSearchOption(optionKey)) {
                continue;
            }

            if (locationOptions[optionKey] === null) {
                this._defaultSearchOptionUsed.add(optionKey);
            }
        }

        return applyDefaultSearchOptions(locationOptions);
    }

    handlePageNavigation(): void {
        this.setState({
            errorValueSubmitted: false,
            optionsCollapsed: true,
            optionsCollapseAnimating: false,
            optionsCollapseAnimateEnabled: false,
        });
    }

    handleSubmit(event: React.FormEvent): void {
        event.preventDefault();
        if (
            this.props.searchQuery.length === 0
            || this.props.searchQuery.length > MAX_QUERY_LENGTH
        ) {
            this.setState({
                errorValueSubmitted: true,
            });
        } else {
            var searchUrl = getSearchUrl({
                query: this.props.searchQuery,
                pageNum: 1,
                options: this.state.options,
            });
            this.props.history.push(searchUrl);
        }
    }

    handleInputtedQueryChange(newValue: string): void {
        function updateState(prevState: State): (
            Pick<State, 'errorValueSubmitted'> | null
        ) {
            if (!prevState.errorValueSubmitted) {
                return null;
            }
            return {
                errorValueSubmitted: false,
            };
        }

        this.setState(updateState);
        this.props.onSearchQueryChange(newValue);
    }

    handleSearchOptionsChange<K extends keyof SearchOptions>(
        changedOption: K, newValue: SearchOptions[K]
    ): void {
        function applyChange(
            this: HeaderSearchForm, prevState: State
        ): Pick<State, 'options'> | null {
            if (newValue === prevState.options[changedOption]) {
                return null;
            }

            setUserSearchOption(changedOption, newValue).catch(
                logNoIndexedDbWarning
            );
            this._defaultSearchOptionUsed.delete(changedOption);
            return {
                options: {
                    ...prevState.options,
                    [changedOption]: newValue,
                },
            };
        }

        this.setState(applyChange.bind(this));
    }

    handleLoadedUserSearchOptions(
        loadedOptions: AllNullable<SearchOptions>
    ): void {
        function applyUserSearchOptions(
            this: HeaderSearchForm, prevState: State
        ): Pick<State, 'options'> {
            var updatedOptions = {...prevState.options};
            for (const optionKey of Object.keys(updatedOptions)) {
                if (!isSearchOption(optionKey)) {
                    continue;
                }

                const loadedOptionValue = loadedOptions[optionKey];
                if (
                    this._defaultSearchOptionUsed.has(optionKey)
                    && loadedOptionValue !== null
                ) {
                    updatedOptions[optionKey] = loadedOptionValue;
                }
            }
            return {
                options: updatedOptions,
            };
        }

        this.setState(applyUserSearchOptions.bind(this));
    }

    handleSearchOptionsCollapseToggle(): void {
        function updateState(prevState: State): (
            Omit<State, 'options' | 'errorValueSubmitted'> | null
        ) {
            if (prevState.optionsCollapseAnimating) {
                return null;
            }

            return {
                optionsCollapsed: !prevState.optionsCollapsed,
                optionsCollapseAnimating: true,
                optionsCollapseAnimateEnabled: true,
            };
        }

        this.setState(updateState);
    }

    handleSearchOptionsCollapseAnimationEnd(): void {
        this.setState({
            optionsCollapseAnimating: false,
        });
    }

    render(): React.ReactElement {
        return (
            <div className='search-container'>
                <form className='search-form' onSubmit={this.handleSubmit}>
                    <SearchBarInput
                        searchQuery={this.props.searchQuery}
                        loading={this.props.loadingSearch}
                        maxQueryLength={MAX_QUERY_LENGTH}
                        errorValueSubmitted={this.state.errorValueSubmitted}
                        onChange={this.handleInputtedQueryChange}
                    />
                    <Collapsable
                        collapsed={this.state.optionsCollapsed}
                        animate={this.state.optionsCollapseAnimateEnabled}
                        onAnimationEnd={
                            this.handleSearchOptionsCollapseAnimationEnd
                        }
                    >
                        <SearchOptionsInput
                            searchOptions={this.state.options}
                            onChange={this.handleSearchOptionsChange}
                        />
                    </Collapsable>
                </form>
                <div className='search-options-toggle-container'>
                    <SearchOptionsCollapseToggle
                        collapsed={this.state.optionsCollapsed}
                        onToggle={this.handleSearchOptionsCollapseToggle}
                    />
                </div>
            </div>
        );
    }
}

export default HeaderSearchForm;