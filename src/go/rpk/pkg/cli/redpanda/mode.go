// Copyright 2021 Redpanda Data, Inc.
//
// Use of this software is governed by the Business Source License
// included in the file licenses/BSL.md
//
// As of the Change Date specified in that file, in accordance with
// the Business Source License, use of this software will be governed
// by the Apache License, Version 2.0

//go:build linux
// +build linux

package redpanda

import (
	"fmt"
	"strings"

	"github.com/redpanda-data/redpanda/src/go/rpk/pkg/config"
	"github.com/redpanda-data/redpanda/src/go/rpk/pkg/out"
	"github.com/spf13/afero"
	"github.com/spf13/cobra"
)

func NewModeCommand(fs afero.Fs, p *config.Params) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "mode [MODE]",
		Short: "Enable a default configuration mode",
		Long:  "",
		Args:  cobra.ExactArgs(1),
		ValidArgsFunction: func(_ *cobra.Command, _ []string, toComplete string) ([]string, cobra.ShellCompDirective) {
			// We complete "dev" and "prod", but if the user is typing
			// the full word, we switch to completing that.
			var complete []string
			for _, s := range []string{config.ModeDev, config.ModeProd, config.ModeRecovery} {
				if strings.HasPrefix(s, toComplete) {
					complete = append(complete, s)
				}
			}
			if len(complete) > 0 {
				return complete, cobra.ShellCompDirectiveDefault
			}
			for _, s := range []string{"development", "production"} {
				if strings.HasPrefix(s, toComplete) {
					complete = append(complete, s)
				}
			}
			return complete, cobra.ShellCompDirectiveDefault
		},
		Run: func(_ *cobra.Command, args []string) {
			err := executeMode(fs, p, args[0])
			out.MaybeDieErr(err)
			fmt.Printf("Successfully set mode to %q.\n", args[0])
		},
	}
	return cmd
}

func executeMode(fs afero.Fs, p *config.Params, mode string) error {
	cfg, err := p.Load(fs)
	if err != nil {
		return fmt.Errorf("unable to load config: %v", err)
	}
	return cfg.SetMode(fs, mode)
}
